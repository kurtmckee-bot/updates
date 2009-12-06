# listparser.py - Parse subscription lists into a consistent format.
# Copyright (C) 2009 Kurt McKee <contactme@kurtmckee.org>
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

__author__ = "Kurt McKee <contactme@kurtmckee.org>"
__url__ = "http://freshmeat.net/projects/listparser"
__version__ = "0.9"

import copy
import datetime
import re
import StringIO
import urllib2
import xml.sax

USER_AGENT = "listparser/%s +%s" % (__version__, __url__)

namespaces = {
    'http://opml.org/spec2': 'opml',
    'http://www.google.com/ig': 'iGoogle',
    'http://schemas.google.com/GadgetTabML/2008': 'gtml',
    'http://www.w3.org/1999/02/22-rdf-syntax-ns#': 'rdf',
    'http://www.w3.org/2000/01/rdf-schema#': 'rdfs',
    'http://xmlns.com/foaf/0.1/': 'foaf',
    'http://purl.org/dc/elements/1.1/': 'dc',
    'http://purl.org/rss/1.0/': 'rss',
}

def _ns(ns):
    return dict(zip(namespaces.values(), namespaces.keys())).get(ns, None)

def parse(filename_or_url, agent=USER_AGENT, etag=None, modified=None, inject=False):
    guarantees = SuperDict({
        'bozo': 0,
        'feeds': [],
        'lists': [],
        'opportunities': [],
        'meta': SuperDict(),
        'version': '',
    })
    fileobj, info = _mkfile(filename_or_url, agent, etag, modified)
    guarantees.update(info)
    if not fileobj:
        return guarantees

    handler = Handler()
    handler.harvest.update(guarantees)
    parser = xml.sax.make_parser()
    parser.setFeature(xml.sax.handler.feature_namespaces, True)
    parser.setContentHandler(handler)
    parser.setErrorHandler(handler)
    if inject:
        parser.parse(Injector(fileobj))
    else:
        parser.parse(fileobj)
    fileobj.close()

    # Test if a DOCTYPE injection is needed
    if hasattr(handler.harvest, 'bozo_exception'):
        if "undefined entity" in handler.harvest.bozo_exception.args:
            if not inject:
                return parse(filename_or_url, agent, etag, modified, True)
    # Make it clear that the XML file is broken
    # (if no other exception has been assigned)
    if inject and not handler.harvest.bozo:
        handler.harvest.bozo = 1
        handler.harvest.bozo_exception = ListError("undefined entity found")
    return handler.harvest

class Handler(xml.sax.handler.ContentHandler, xml.sax.handler.ErrorHandler):
    def __init__(self):
        xml.sax.handler.ContentHandler.__init__(self)
        self.harvest = SuperDict()
        self.expect = False
        self._characters = unicode()
        self.hierarchy = []
        self.flag_feed = False
        self.flag_opportunity = False
        self.flag_group = False
        # found_urls = {url: (append_to_key, obj)}
        self.found_urls = SuperDict()
        # group_objs = [(append_to_key, obj)]
        self.group_objs = []
        self.agent_feeds = []
        self.agent_opps = []
        self.foaf_name = unicode()

    def raise_bozo(self, err):
        self.harvest.bozo = 1
        if isinstance(err, basestring):
            self.harvest.bozo_exception = ListError(err)
        else:
            self.harvest.bozo_exception = err

    # ErrorHandler functions
    def warning(self, exception):
        self.raise_bozo(exception)
        return
    error = warning
    fatalError = warning

    # ContentHandler functions
    def startElementNS(self, name, qname, attrs):
        fn = ''
        if name[0] in namespaces:
            fn = '_start_%s_%s' % (namespaces[name[0]], name[1])
        elif name[0] is None:
            fn = '_start_opml_%s' % (name[1])
        if hasattr(getattr(self, fn, None), '__call__'):
            getattr(self, fn)(attrs)

    def endElementNS(self, name, qname):
        fn = ''
        if name[0] in namespaces:
            fn = '_end_%s_%s' % (namespaces[name[0]], name[1])
        elif name[0] is None:
            fn = '_end_opml_%s' % (name[1])
        if hasattr(getattr(self, fn, None), '__call__'):
            getattr(self, fn)()
            # Always disable and reset character capture in order to
            # reduce code duplication in the _end_opml_* functions
            self.expect = False
            self._characters = unicode()

    def characters(self, content):
        if self.expect:
            self._characters += content

    # OPML support
    #--------------

    def _start_opml_opml(self, attrs):
        self.harvest.version = "opml"
        if attrs.get((None, 'version')) in ("1.0", "1.1"):
            self.harvest.version = "opml1"
        elif attrs.get((None, 'version')) == "2.0":
            self.harvest.version = "opml2"

    def _start_opml_outline(self, attrs):
        url = title = None
        # Find an appropriate title in @text or @title
        if attrs.get((None, 'text'), '').strip():
            title = attrs[(None, 'text')].strip()
        else:
            if attrs.get((None, 'title'), '').strip():
                title = attrs[(None, 'title')].strip()

        # Determine whether the outline is a feed or subscription list
        if 'xmlurl' in (i[1].lower() for i in attrs.keys()):
            # It's a feed
            append_to = 'feeds'
            if attrs.get((None, 'type'), '').strip().lower() == 'source':
                # Actually, it's a subscription list!
                append_to = 'lists'
            # Get the URL regardless of xmlUrl's case
            for k, v in attrs.items():
                if k[1].lower() == 'xmlurl':
                    url = v.strip()
                    break
        elif attrs.get((None, 'type'), '').lower() in ('link', 'include'):
            # It's a subscription list
            append_to = 'lists'
            url = attrs[(None, 'url')].strip()
        elif title is not None:
            # Assume that this is a grouping node
            self.hierarchy.append(title)
            return
        # Look for an opportunity URL
        if not url and 'htmlurl' in (k[1].lower() for k in attrs.keys()):
            for k, v in attrs.items():
                if k[1].lower() == 'htmlurl':
                    url = v.strip()
            append_to = 'opportunities'
        if not url:
            # Maintain the hierarchy
            self.hierarchy.append('')
            return
        if url not in self.found_urls:
            # This is a brand new URL
            obj = SuperDict({'url': url})
            if title is not None:
                obj.title = title
            self.found_urls[url] = (append_to, obj)
            self.harvest[append_to].append(obj)
        else:
            obj = self.found_urls[url][1]

        # Handle categories and tags
        tags = []
        cats = []
        if attrs.has_key((None, 'category')):
            for i in attrs[(None, 'category')].split(','):
                if '/' not in i and i.strip():
                    tags.append(i.strip())
                elif '/' in i:
                    tmp = [j.strip() for j in i.split('/') if j.strip()]
                    if tmp and tmp not in cats:
                        cats.append(tmp)
        # Copy the current hierarchy into `categories`
        if self.hierarchy and self.hierarchy not in cats:
            cats.append(copy.copy(self.hierarchy))
        # Copy all single-element `categories` into `tags`
        tags.extend([i[0] for i in cats if len(i) == 1])

        if tags:
            obj.setdefault('tags', []).extend(tags)
            obj.tags = list(set(obj.tags))
        if cats:
            obj.setdefault('categories', [])
            for cat in cats:
                if cat not in obj.categories:
                    obj.categories.append(cat)

        self.hierarchy.append('')
    def _end_opml_outline(self):
        self.hierarchy.pop()

    def _expect_characters(self, attrs):
        # Most _start_opml_* functions only need to set these two variables,
        # so this function exists to reduce significant code duplication
        self.expect = True
        self._characters = unicode()

    _start_opml_title = _expect_characters
    def _end_opml_title(self):
        if self._characters.strip():
            self.harvest.setdefault('meta', SuperDict())
            self.harvest.meta.title = self._characters.strip()

    _start_opml_ownerId = _expect_characters
    def _end_opml_ownerId(self):
        if self._characters.strip():
            self.harvest.setdefault('meta', SuperDict())
            self.harvest.meta.setdefault('author', SuperDict())
            self.harvest.meta.author.url = self._characters.strip()

    _start_opml_ownerEmail = _expect_characters
    def _end_opml_ownerEmail(self):
        if self._characters.strip():
            self.harvest.setdefault('meta', SuperDict())
            self.harvest.meta.setdefault('author', SuperDict())
            self.harvest.meta.author.email = self._characters.strip()

    _start_opml_ownerName = _expect_characters
    def _end_opml_ownerName(self):
        if self._characters.strip():
            self.harvest.setdefault('meta', SuperDict())
            self.harvest.meta.setdefault('author', SuperDict())
            self.harvest.meta.author.name = self._characters.strip()

    _start_opml_dateCreated = _expect_characters
    def _end_opml_dateCreated(self):
        if self._characters.strip():
            self.harvest.setdefault('meta', SuperDict())
            self.harvest.meta.created = self._characters.strip()
            d = _rfc822(self.harvest.meta.created)
            if isinstance(d, datetime.datetime):
                self.harvest.meta.created_parsed = d
            else:
                self.raise_bozo('dateCreated is not an RFC 822 datetime')

    _start_opml_dateModified = _expect_characters
    def _end_opml_dateModified(self):
        if self._characters.strip():
            self.harvest.setdefault('meta', SuperDict())
            self.harvest.meta.modified = self._characters.strip()
            d = _rfc822(self.harvest.meta.modified)
            if isinstance(d, datetime.datetime):
                self.harvest.meta.modified_parsed = d
            else:
                self.raise_bozo('dateModified is not an RFC 822 datetime')

    # iGoogle/GadgetTabML support
    #-----------------------------

    def _start_gtml_GadgetTabML(self, attrs):
        self.harvest.version = 'igoogle'

    def _start_gtml_Tab(self, attrs):
        if attrs.get((None, 'title'), '').strip():
            self.hierarchy.append(attrs[(None, 'title')].strip())
    def _end_gtml_Tab(self):
        self.hierarchy.pop()

    def _start_iGoogle_Module(self, attrs):
        if attrs.get((None, 'type'), '').strip().lower() == 'rss':
            self.flag_feed = True
    def _end_iGoogle_Module(self):
        self.flag_feed = False

    def _start_iGoogle_ModulePrefs(self, attrs):
        if self.flag_feed and attrs.get((None, 'xmlUrl'), '').strip():
            obj = SuperDict({'url': attrs[(None, 'xmlUrl')].strip()})
            if self.hierarchy:
                obj.categories = [copy.copy(self.hierarchy)]
            if len(self.hierarchy) == 1:
                obj.tags = copy.copy(self.hierarchy)
            self.harvest.feeds.append(obj)

    # RDF+FOAF support
    #------------------

    def _start_rdf_RDF(self, attrs):
        self.harvest.version = 'rdf'

    def _start_rss_channel(self, attrs):
        if attrs.get((_ns('rdf'), 'about'), '').strip():
            # We now have a feed URL, so forget about any opportunity URL
            if self.flag_opportunity:
                self.flag_opportunity = False
                self.agent_opps.pop()
            self.agent_feeds.append(attrs.get((_ns('rdf'), 'about')).strip())

    def _start_foaf_Agent(self, attrs):
        self.flag_feed = True
    def _end_foaf_Agent(self):
        for url in self.agent_feeds:
            obj = SuperDict({'url': url, 'title': self.foaf_name})
            self.group_objs.append(('feeds', obj))
        for url in self.agent_opps:
            obj = SuperDict({'url': url, 'title': self.foaf_name})
            self.group_objs.append(('opportunities', obj))
        self.foaf_name = ''
        self.agent_feeds = []
        self.agent_opps = []
        self.flag_feed = False
        self.flag_opportunity = False

    def _start_foaf_Group(self, attrs):
        self.flag_group = True
    def _end_foaf_Group(self):
        self.flag_group = False
        for key, obj in self.group_objs:
            # Check for duplicates
            if obj.url in self.found_urls:
                obj = self.found_urls[obj.url][1]
            else:
                self.found_urls[obj.url] = (key, obj)
                self.harvest[key].append(obj)
            # Create or consolidate categories and tags
            obj.setdefault('categories', [])
            obj.setdefault('tags', [])
            if self.hierarchy and self.hierarchy not in obj.categories:
                obj.categories.append(copy.copy(self.hierarchy))
            if len(self.hierarchy) == 1 and \
               self.hierarchy[0] not in obj.tags:
                obj.tags.extend(copy.copy(self.hierarchy))
        self.group_objs = []
        # Maintain the hierarchy
        if self.hierarchy:
            self.hierarchy.pop()
    _end_rdf_RDF = _end_foaf_Group

    _start_foaf_name = _expect_characters
    def _end_foaf_name(self):
        if self.flag_feed:
            self.foaf_name = self._characters.strip()
        elif self.flag_group and self._characters.strip():
            self.hierarchy.append(self._characters.strip())
            self.flag_group = False

    def _start_foaf_Document(self, attrs):
        if attrs.get((_ns('rdf'), 'about'), '').strip():
            # Flag this as an opportunity (but ignore if a feed URL is found)
            self.flag_opportunity = True
            self.agent_opps.append(attrs.get((_ns('rdf'), 'about')).strip())

class HTTPRedirectHandler(urllib2.HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, hdrs):
        result = urllib2.HTTPRedirectHandler.http_error_301(self, req, fp,
                                                            code, msg, hdrs)
        result.status = code
        result.newurl = result.geturl()
        return result
    # The default implementations in urllib2.HTTPRedirectHandler
    # are identical, so hardcoding a http_error_301 call above
    # won't affect anything
    http_error_302 = http_error_303 = http_error_307 = http_error_301

class HTTPErrorHandler(urllib2.HTTPDefaultErrorHandler):
    def http_error_default(self, req, fp, code, msg, hdrs):
        # The default implementation just raises HTTPError.
        # Forget that.
        fp.status = code
        return fp

def _mkfile(obj, agent, etag, modified):
    if hasattr(obj, 'read') and hasattr(obj, 'close'):
        # It's file-like
        return obj, SuperDict()
    elif not isinstance(obj, basestring):
        # This isn't a known-parsable object
        err = ListError('parse() called with unparsable object')
        return None, SuperDict({'bozo': 1, 'bozo_exception': err})
    if obj.find('\n') != -1 or not obj.find('://') in (3, 4, 5):
        # It's not a URL; make the string a file
        return StringIO.StringIO(obj), SuperDict()
    # It's a URL
    headers = {'User-Agent': agent}
    if isinstance(etag, basestring):
        headers['If-None-Match'] = etag
    if isinstance(modified, basestring):
        headers['If-Modified-Since'] = modified
    elif isinstance(modified, datetime.datetime):
        # It is assumed that `modified` is in UTC time
        headers['If-Modified-Since'] = _to_rfc822(modified)
    request = urllib2.Request(obj, headers=headers)
    opener = urllib2.build_opener(HTTPRedirectHandler, HTTPErrorHandler)
    try:
        ret = opener.open(request)
    except urllib2.URLError, err:
        return None, SuperDict({'bozo': 1, 'bozo_exception': err})

    info = SuperDict({'status': getattr(ret, 'status', 200)})
    info.href = getattr(ret, 'newurl', obj)
    info.headers = SuperDict(getattr(ret, 'headers', {}))
    # Python 3 doesn't normalize tag names; Python 2 does
    if info.headers.get('ETag') or info.headers.get('etag'):
        info.etag = info.headers.get('ETag') or info.headers.get('etag')
    if info.headers.get('Last-Modified') or info.headers.get('last-modified'):
        info.modified = info.headers.get('Last-Modified') or \
                        info.headers.get('last-modified')
        if isinstance(_rfc822(info.modified), datetime.datetime):
            info.modified_parsed = _rfc822(info.modified)
    return ret, info


def _rfc822(date):
    """Parse RFC 822 dates and times, with one minor
    difference: years may be 4DIGIT or 2DIGIT.
    http://tools.ietf.org/html/rfc822#section-5"""
    months = ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
              'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
    daynames = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

    month_ = "(?P<month>%s)" % ('|'.join(months))
    year_ = "(?P<year>(?:\d{2})?\d{2})"
    day_ = "(?P<day>\d{2})"
    date_ = "%s %s %s" % (day_, month_, year_)
    
    hour_ = "(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    tz_ = "(?P<tz>ut|gmt|[ecmp][sd]t|[zamny]|[+-]\d{4})"
    time_ = "%s %s" % (hour_, tz_)

    dayname_ = "(?P<dayname>%s)" % ('|'.join(daynames))
    dt_ = "(?:%s, )?%s %s" % (dayname_, date_, time_)

    try:
        m = re.match(dt_, date.lower()).groupdict(0)
    except:
        return None

    # Calculate a date and timestamp
    for k in ('year', 'day', 'hour', 'minute', 'second'):
        m[k] = int(m[k])
    m['month'] = months.index(m['month']) + 1
    # If the year is 2 digits, assume everything in the 90's is the 1990's
    if m['year'] < 100:
        m['year'] += (1900, 2000)[m['year'] < 90]
    try:
        stamp = datetime.datetime(*[m[i] for i in 
                    ('year', 'month', 'day', 'hour', 'minute', 'second')])
    except ValueError:
        return None

    # Use the timezone information to calculate the difference between
    # the given date and timestamp and Universal Coordinated Time
    if m['tz'].startswith('+'):
        tzhour = int(m['tz'][1:3])
        tzmin = int(m['tz'][3:])
    elif m['tz'].startswith('-'):
        tzhour = int(m['tz'][1:3]) * -1
        tzmin = int(m['tz'][3:]) * -1
    else:
        tzinfo = {
                    'ut': 0, 'gmt': 0, 'z': 0,
                    'edt': -4, 'est': -5,
                    'cdt': -5, 'cst': -6,
                    'mdt': -6, 'mst': -7,
                    'pdt': -7, 'pst': -8,
                    'a': -1, 'n': 1,
                    'm': -12, 'y': 12,
                 }
        tzhour = tzinfo[m['tz']]
        tzmin = 0
    delta = datetime.timedelta(0, 0, 0, 0, tzmin, tzhour)

    # Return the date and timestamp in UTC
    try:
        return stamp - delta
    except OverflowError:
        return None

def _to_rfc822(date):
    """_to_rfc822(datetime.datetime) -> str
    The datetime `strftime` method is subject to locale-specific
    day and month names, so this function hardcodes the conversion."""
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    fmt = '%(day)s, %(d)02i %(month)s %(y)04i %(h)02i:%(m)02i:%(s)02i GMT'
    return fmt % {
                    'day': days[date.weekday()],
                    'd': date.day,
                    'month': months[date.month - 1],
                    'y': date.year,
                    'h': date.hour,
                    'm': date.minute,
                    's': date.second,
                 }

class SuperDict(dict):
    """
    SuperDict is a dictionary object with keys posing as instance attributes.

    >>> i = SuperDict()
    >>> i.one = 1
    >>> i
    {'one': 1}
    """

    def __getattribute__(self, name):
        if name in self:
            return self[name]
        else:
            return dict.__getattribute__(self, name)

    def __setattr__(self, name, value):
        self[name] = value
        return value

class Injector(object):
    """
    Injector buffers the first read() call to a file-like object in
    order to inject a DOCTYPE containing entity definitions immediately
    following the XML declaration.
    """
    def __init__(self, obj):
        self.obj = obj
        self.injected = False
    def read(self, size=None):
        if size is not None:
            read = self.obj.read(size)
        else:
            read = self.obj.read()
        if self.injected:
            return read
        # Inject ENTITY references at the start of the document
        import htmlentitydefs
        entities = unicode()
        for k, v in htmlentitydefs.name2codepoint.items():
            entities += '<!ENTITY %s "&#%s;">' % (k, v)
        doctype = "<!DOCTYPE anyroot [%s]>" % (entities, )
        lines = read.splitlines()
        lines.insert(1, bytestr(doctype))
        self.injected = True
        return bytestr('\n').join(lines)
    def __getattr__(self, name):
        return getattr(self.obj, name)
    def __hasattr__(self, name):
        return hasattr(self.obj, name)

class ListError(Exception):
    """Used when a specification deviation is encountered in an XML file"""
    pass

def bytestr(text):
    # HACK: force `text` to the type expected by Python 2 and Python 3
    # Python 2 expects type(basestring)
    # Python 3 expects type(bytes)
    try:
        return bytes(text, 'utf8')
    except (TypeError, NameError):
        return text
