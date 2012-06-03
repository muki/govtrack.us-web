"""
Parser of:
 * bill terms located in data/us/[liv, liv111, crsnet].xml
 * bills located in data/us/*/bills/*.xml
"""
import logging
import csv, os, hashlib, random
from datetime import datetime

from parser.progress import Progress
from parser.models import File
from states.models import StateChamberEnum, StateLegislator, StateSubjectTerm, StateSession, StateBill, StateBillAction, StateBillDocument

from django.db.utils import IntegrityError

csv.field_size_limit(1000000000) # _csv.Error: field larger than field limit (131072)

log = logging.getLogger('parser.states_parser')

# Utility functions to cache objects and only save when the objects are popped from the cache.
cached_objs = { }
def cached_objs_pop(haystack_index):
	global cached_objs
	key = random.choice(list(cached_objs))
	obj = cached_objs[key]
	if getattr(obj, "needs_save", False):
		obj.save()
		if isinstance(obj, StateBill):
			obj.create_events()
			if haystack_index: haystack_index.update_object(obj, using="states")
		obj.needs_save = False
	del cached_objs[key]
def cached_objs_gc(haystack_index):
	global cached_objs
	while len(cached_objs) > 128:
		cached_objs_pop(haystack_index)
def cached_objs_clear(haystack_index):
	global cached_objs
	while len(cached_objs) > 0:
		cached_objs_pop(haystack_index)


# Utility class to read non-ASCII CSV files and return a decoded (unicode) dict for each row.
def UnicodeDictReader(stream, encoding, **kwargs):
    csv_reader = csv.DictReader(stream, **kwargs)
    for row in csv_reader:
        yield dict((key, value.decode(encoding, "replace") if isinstance(value, str) else value) for key, value in row.iteritems())

# Decorator wraps function in a check for whether the source file has been modified.
def iffilechanged(func):
	def g(options, filename, *args, **kwargs):
		if not File.objects.is_changed(filename) and not options.force:
			return None
		ret = func(options, filename, *args, **kwargs)
		File.objects.save_file(filename)
		return ret
	return g

# Decorator wraps a function to which we pass each row in a CSV file one by one.
def rowbyrow(func):
	def g(options, filename, *args, **kwargs):
		f = open(filename, "rb")
		
		progress = Progress(name='rows [%s]' % filename, step=2000)
		
		f.seek(0, os.SEEK_END)
		total=f.tell()
		f.seek(0, os.SEEK_SET)

		for row in UnicodeDictReader(f, "cp1252", quotechar='|' if "Subject" not in filename and "ActionHistory" not in filename else '"'):
			row["_hash"] = hashlib.sha1(repr(sorted(row.items()))).hexdigest()
			progress.tick(x=f.tell(), y=total)
			func(row, options, filename, *args, **kwargs)
		return None
	return g

@iffilechanged
@rowbyrow
def process_legislators(row, options, filename):
	try:
		p = StateLegislator.objects.get(bt50id = row["LegislatorID"])
	except StateLegislator.DoesNotExist:
		p = StateLegislator()
		p.bt50id = row["LegislatorID"]
		
	if row["SunlightLegislatorID"] not in ("", "0"): p.openstatesid = row["SunlightLegislatorID"]
	if row["LegiScanLegislatorID"] != "": p.legiscanid = row["LegiScanLegislatorID"]
	
	p.state = row["StateCode"]
	p.firstname = row["FirstName"]
	p.lastname = row["LastName"]
	p.fullname = row["LegislatorName"]
	p.party = row["LegislatorParty"]
	
	p.save()

@iffilechanged
@rowbyrow
def process_subjects(row, options, filename):
	if row["StateSubjectID"] == "0":
		# Ignore the "unknown" term.
		return
	
	try:
		s = StateSubjectTerm.objects.get(bt50id = row["StateSubjectID"])
	except StateSubjectTerm.DoesNotExist:
		s = StateSubjectTerm()
		s.bt50id = row["StateSubjectID"]
		
	s.state = row["StateCode"]
	s.name = row["StateSubject"]
	
	s.save()

# TODO: unicameral?
chamber_map = { "Lower": StateChamberEnum.lower, "Upper": StateChamberEnum.upper, "": StateChamberEnum.unknown }

@iffilechanged
@rowbyrow
def process_bills(row, options, filename, haystack_index):
	
	# dupes
	if row["BillID"] in ("207399", "207400"): return
	
	try:
		b = StateBill.objects.get(bt50id = row["BillID"])
		if b.srchash == row["_hash"]: return
	except StateBill.DoesNotExist:
		b = StateBill()
		b.bt50id = row["BillID"]
	
	if row["SunlightBillID"] != "": b.openstatesid = row["SunlightBillID"]
	if row["LegiScanBillID"] != "": b.legiscanid = row["LegiScanBillID"]
	
	session, isnew = StateSession.objects.get_or_create(
		state=row["StateCode"],
		name=row["IntroducedSession"],
		defaults={
			"slug": hashlib.sha1(row["IntroducedSession"]).hexdigest()[0:12]
		})
	b.state_session = session
	b.bill_number = row["StateBillID"]
	b.chamber = chamber_map[row["IntroducedChamber"]]
	if row["StateCode"] in ("NE", "DC"): b.chamber = StateChamberEnum.unicameral # DC not in yet, but later?
	
	b.short_title = row["ShortBillName"]
	b.long_title = row["FullBillName"]
	b.summary = row["BillSummary"]
	
	b.srchash = row["_hash"]
	
	try:
		b.save() # save object instance
		if haystack_index: haystack_index.update_object(b, using="states") # index the full text
		if not options.disable_events: b.create_events() # create events to track
		
	except ValueError:
		import pprint
		pprint.pprint(row)
		#raise
	except IntegrityError as e:
		# For the sake of first import, skip errors.
		print row["BillID"], repr(e)

@iffilechanged
@rowbyrow
def process_bill_actions(row, options, filename, haystack_index):
	# Because the actions file has many entries for the same bill in a row,
	# try to cache the bill objects from call to call, and try to hold off
	# on saving bill instances until we pop them from the cache.

	global cached_objs

	if "b:" + row["BillID"] in cached_objs:
		b = cached_objs["b:" + row["BillID"]]
	else:
		cached_objs_gc(haystack_index)
		try:
			b = StateBill.objects.get(bt50id = row["BillID"])
		except StateBill.DoesNotExist as e:
			# Ignore parse errors.
			print e
			return
		cached_objs["b:" + row["BillID"]] = b
	
	when = datetime.strptime(row["ActionDate"], '%Y-%m-%d %H:%M:%S')
	seq = int(row["ActionOrder"])
	
	if not b.introduced_date or when.date() < b.introduced_date:
		b.introduced_date = when.date()
		b.needs_save = True # will be saved when popped from cache
	if not b.last_action_date or when.date() > b.last_action_date or (when.date() == b.last_action_date and seq > b.last_action_seq):
		b.last_action_date = when.date()
		b.last_action_seq = seq
		b.last_action_text = row["ActionDescription"]
		b.needs_save = True # will be saved when popped from cache

	# # Initialize StateSession start/end dates using bill action dates.
	# if "s:" + str(b.state_session_id) in cached_objs:
		# ss = cached_objs["s:" + str(b.state_session_id)]
	# else:
		# cached_objs_gc(haystack_index)
		# ss = b.state_session
		# cached_objs["s:" + str(ss.id)] = ss
	# if not ss.startdate or when.date() < ss.startdate:
		# ss.startdate = when.date()
		# ss.needs_save = True
	# if not ss.enddate or when.date() > ss.enddate:
		# ss.enddate = when.date()
		# ss.needs_save = True

	try:
		s = StateBillAction.objects.get(bt50id = row["ActionHistoryID"])
		if not options.force: return # assume actions don't change once created, unless --force is used
	except StateBillAction.DoesNotExist:
		s = StateBillAction()
		s.bt50id = row["ActionHistoryID"]
		
	save = not s.id or (s.bill != b or s.seq != seq or s.date != when or s.text != row["ActionDescription"])
		
	s.bill = b
	s.seq = seq
	s.date = when
	s.text = row["ActionDescription"]
	
	if not s.id or save:
		s.save()
		
		# trigger the creation of events, held until bill leaves cache
		b.needs_save = True

@iffilechanged
@rowbyrow
def process_bill_documents(row, options, filename, haystack_index):
	global cached_objs

	if "b:" + row["BillID"] in cached_objs:
		b = cached_objs["b:" + row["BillID"]]
	else:
		cached_objs_gc(haystack_index)
		try:
			b = StateBill.objects.get(bt50id = row["BillID"])
		except StateBill.DoesNotExist as e:
			# Ignore parse errors.
			print e
			return
		cached_objs["b:" + row["BillID"]] = b
	
	try:
		s = StateBillDocument.objects.get(bt50id = row["DocumentID"])
		if not options.force: return # assume documents don't change once created, unless --force is used
	except StateBillDocument.DoesNotExist:
		s = StateBillDocument()
		s.bt50id = row["DocumentID"]
		
	save = not s.id or(s.bill != b or s.type != row["DocumentName"] or s.url != row["DocumentPath"])
		
	s.bill = b
	s.type = row["DocumentName"]
	s.url = row["DocumentPath"]
	
	if not s.id or save:
		s.save()
	
def main(options):
    """
    Process state legislative data.
    """
    
    haystack_index = None
    if not options.disable_indexing:
        from states.search_indexes import StateBillIndex
        haystack_index = StateBillIndex()

    process_legislators(options, "../extdata/billtrack50/tLegislator.txt")
    process_subjects(options, "../extdata/billtrack50/tStateSubject.txt")
    process_bills(options, "../extdata/billtrack50/tBill.txt", haystack_index)
    process_bill_documents(options, "../extdata/billtrack50/tDocument.txt", haystack_index)
    process_bill_actions(options, "../extdata/billtrack50/tActionHistory.txt", haystack_index)
    
    cached_objs_clear(haystack_index)

