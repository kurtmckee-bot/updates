# listparser.py - Parse OPML subscription lists into a negotiable format.
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

__version__ = "0.6"

import copy
import datetime
import re
import StringIO
import urllib2
import xml.sax

USER_AGENT = "listparser/%s +http://freshmeat.net/projects/listparser" % (__version__)

def parse(filename_or_url, agent=USER_AGENT, etag=None, modified=None):
    guarantees = SuperDict({
        'bozo': 0,
        'feeds': [],
        'lists': [],
        'meta': SuperDict(),
        'version': None,
    })
    fileobj, info = _mkfile(filename_or_url, agent, etag, modified)
    guarantees.update(info)
    if not fileobj:
        return guarantees

    handler = Handler()
    handler.harvest.update(guarantees)
    parser = xml.sax.make_parser()
    parser.setContentHandler(handler)
    parser.setErrorHandler(handler)
    parser.parse(fileobj)
    fileobj.close()

    return handler.harvest

class Handler(xml.sax.handler.ContentHandler, xml.sax.handler.ErrorHandler):
    def __init__(self):
        xml.sax.handler.ContentHandler.__init__(self)
        self.harvest = SuperDict()
        self.expect = ''
        self.hierarchy = []

    # ErrorHandler functions
    def warning(self, exception):
        self.harvest.bozo = 1
        self.harvest.bozo_exception = repr(exception)
        return
    error = warning
    fatalError = warning

    # ContentHandler functions
    def startElement(self, name, attrs):
        if hasattr(self, '_start_%s' % name):
            getattr(self, '_start_%s' % name)(attrs)
    def endElement(self, name):
        if hasattr(self, '_end_%s' % name):
            getattr(self, '_end_%s' % name)()
    def characters(self, content):
        if not self.expect:
            return
        # If `expect` contains something like "userinfo_contact_email_domain",
        # then after these next lines, node will point to the nested dictionary
        # `self.harvest['userinfo']['contact']['email']`, and
        # `...['email']['domain']` will be filled with `content`.
        node = reduce(lambda x, y: x.setdefault(y, SuperDict()), self.expect.split('_')[:-1], self.harvest)
        node[self.expect.split('_')[-1]] = node.setdefault(self.expect.split('_')[-1], '') + content
    def _start_opml(self, attrs):
        self.harvest.version = "opml"
        if attrs.has_key('version'):
            if attrs['version'] in ("1.0", "1.1"):
                self.harvest.version = "opml1"
            elif attrs['version'] == "2.0":
                self.harvest.version = "opml2"
            else:
                self.harvest.bozo = 1
                self.harvest.bozo_exception = "Unknown OPML version"
        else:
            self.harvest.bozo = 1
            self.harvest.bozo_exception = "<opml> MUST have a version attribute"
    def _start_outline(self, attrs):
        url = title = None
        # Find an appropriate title in @text or @title
        if attrs.has_key('text') and attrs.get('text', '').strip():
            title = attrs['text'].strip()
        else:
            self.harvest.bozo = 1
            self.harvest.bozo_exception = "An <outline> has a missing or empty `text` attribute"
            if attrs.has_key('title') and attrs.get('title', '').strip():
                title = attrs['title'].strip()

        # Determine whether the outline is a feed or subscription list
        if 'xmlurl' in (i.lower() for i in attrs.keys()):
            # It's a feed
            append_to = self.harvest.feeds
            if not attrs.has_key('type'):
                self.harvest.bozo = 1
                self.harvest.bozo_exception = "<outline> MUST have a `type` attribute"
            elif attrs['type'].lower() != 'rss':
                self.harvest.bozo = 1
                self.harvest.bozo_exception = "//outline/@type is not recognized"
            if not attrs.has_key('xmlUrl'):
                self.harvest.bozo = 1
                self.harvest.bozo_exception = "Only `xmlUrl` EXACTLY is valid"
            # This generator expression selects the `xmlUrl` attribute no matter its case
            url = (v.strip() for k, v in attrs.items() if k.lower() == "xmlurl").next()
        elif attrs.has_key('url') and attrs.get('type', '').lower() in ('link', 'include'):
            # It's a subscription list
            append_to = self.harvest.lists
            url = attrs['url'].strip()
            if attrs['type'].lower() == 'link' and not url.endswith('.opml'):
                self.harvest.bozo = 1
                self.harvest.bozo_exception = "`link` types' `url` attribute MUST end with '.opml'"
        elif attrs.get('type', '').strip().lower() in ('rss', 'link', 'include'):
            # It *should* be a feed or subscription list, but it has no URL
            self.harvest.bozo = 1
            self.harvest.bozo_exception = "no URL found for rss, link, or include type"
            self.hierarchy.append('')
            return
        elif title is not None:
            # Assume that this is a grouping node
            self.hierarchy.append(title)
            return
        if not url:
            self.harvest.bozo = 1
            self.harvest.bozo_exception = "no URL found"
            self.hierarchy.append('')
            return
        obj = SuperDict({'url': url})
        if title is not None:
            obj.title = title

        # Handle categories and tags
        if attrs.has_key('category'):
            def or_strip(x, y):
                return x.strip() or y.strip()
            tags = [x.strip() for x in attrs['category'].split(',') if x.strip() and '/' not in x]
            cats = (x.strip() for x in attrs['category'].split(',') if '/' in x)
            cats = (x.split('/') for x in cats if reduce(or_strip, x.split('/')))
            cats = (xlist for xlist in cats if reduce(or_strip, xlist))
            cats = [[y.strip() for y in xlist if y.strip()] for xlist in cats]
            if tags:
                obj.tags = tags
            if cats:
                obj.categories = cats
        # Copy the current hierarchy into `categories`
        if self.hierarchy and self.hierarchy not in obj.get('categories', []):
            obj.setdefault('categories', []).append(copy.copy(self.hierarchy))
        # Copy all single-element `categories` into `tags`
        tags = [i[0] for i in obj.get('categories', []) if len(i) == 1 and i[0] not in obj.get('tags', [])]
        if tags:
            obj.setdefault('tags', []).extend(tags)
        # Fill obj.claims up with information that is *purported* to
        # be duplicated from the feed itself.
        for k in ('htmlUrl', 'title', 'description'):
            if attrs.has_key(k):
                obj.setdefault('claims', SuperDict())[k] = attrs[k].strip()
        append_to.append(obj)
        self.hierarchy.append('')
    def _end_outline(self):
        self.hierarchy.pop()
    def _start_title(self, attrs):
        self.expect = 'meta_title'
    def _end_title(self):
        if self.harvest.meta.get('title', False):
            self.harvest.meta.title = self.harvest.meta.title.strip()
        self.expect = ''
    def _start_ownerId(self, attrs):
        self.expect = 'meta_author_url'
    def _end_ownerId(self):
        if self.harvest.meta.get('author', SuperDict()).get('url', False):
            self.harvest.meta.author.url = self.harvest.meta.author.url.strip()
        self.expect = ''
    def _start_ownerEmail(self, attrs):
        self.expect = 'meta_author_email'
    def _end_ownerEmail(self):
        if self.harvest.meta.get('author', SuperDict()).get('email', False):
            self.harvest.meta.author.email = self.harvest.meta.author.email.strip()
        self.expect = ''
    def _start_ownerName(self, attrs):
        self.expect = 'meta_author_name'
    def _end_ownerName(self):
        if self.harvest.meta.get('author', SuperDict()).get('name', False):
            self.harvest.meta.author.name = self.harvest.meta.author.name.strip()
        self.expect = ''
    def _start_dateCreated(self, attrs):
        self.expect = 'meta_created'
    def _end_dateCreated(self):
        if self.harvest.meta.get('created', '').strip():
            self.harvest.meta.created = self.harvest.meta.created.strip()
            d = _rfc822(self.harvest.meta.created.strip())
            if isinstance(d, datetime.datetime):
                self.harvest.meta.created_parsed = d
            else:
                self.harvest.bozo = 1
                self.harvest.bozo_exception = "dateCreated is not a valid datetime"
        self.expect = ''
    def _start_dateModified(self, attrs):
        self.expect = 'meta_modified'
    def _end_dateModified(self):
        if self.harvest.meta.get('modified', '').strip():
            self.harvest.meta.modified = self.harvest.meta.modified.strip()
            d = _rfc822(self.harvest.meta.modified.strip())
            if isinstance(d, datetime.datetime):
                self.harvest.meta.modified_parsed = d
            else:
                self.harvest.bozo = 1
                self.harvest.bozo_exception = "dateModified is not a valid datetime"
        self.expect = ''

class HTTPRedirectHandler(urllib2.HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, hdrs):
        result = urllib2.HTTPRedirectHandler.http_error_301(self, req, fp, code, msg, hdrs)
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
        return None, SuperDict({'bozo': 1, 'bozo_exception': 'unparsable object'})
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
        headers['If-Modified-Since'] = modified.strftime('%a, %d %b %Y %H:%M:%S GMT')
    try:
        request = urllib2.Request(obj, headers=headers)
        opener = urllib2.build_opener(HTTPRedirectHandler, HTTPErrorHandler)
        ret = opener.open(request)
    except:
        return None, SuperDict()

    info = SuperDict({'status': getattr(ret, 'status', 200)})
    info.href = getattr(ret, 'newurl', obj)
    info.headers = SuperDict(getattr(ret, 'headers', {}))
    if info.headers.get('etag'):
        info.etag = info.headers.get('etag')
    if info.headers.get('last-modified'):
        info.modified = info.headers['last-modified']
        if _rfc822(info.headers['last-modified']):
            info.modified_parsed = _rfc822(info.headers['last-modified'])
    return ret, info


def _rfc822(date):
    """Parse RFC 822 dates and times, with one minor
    difference: years may be 4DIGIT or 2DIGIT.
    http://tools.ietf.org/html/rfc822#section-5"""
    month_ = "(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    year_ = "(?P<year>(?:\d{2})?\d{2})"
    day_ = "(?P<day>\d{2})"
    date_ = "%s %s %s" % (day_, month_, year_)
    
    hour_ = "(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    tz_ = "(?P<tz>ut|gmt|[ecmp][sd]t|[zamny]|[+-]\d{4})"
    time_ = "%s %s" % (hour_, tz_)

    dayname_ = "(?P<dayname>mon|tue|wed|thu|fri|sat|sun)"
    dt_ = "(?:%s, )?%s %s" % (dayname_, date_, time_)

    try:
        m = re.match(dt_, date.lower()).groupdict(0)
    except:
        return None
    # directly convert everything listed into an int
    m.update((x, int(m[x])) for x in ('year', 'day', 'hour', 'minute', 'second'))
    # convert month to an int in the range 1..12
    m['month'] = (month_.index(m['month']) - 9) // 4 + 1
    # ensure year is 4 digits; assume everything in the 90's is the 1990's
    if m['year'] < 100:
        m['year'] += (1900, 2000)[m['year'] < 90]
    if m['tz'][0] in '+-':
        tzhour, tzmin = int(m['tz'][1:-2]), int(m['tz'][-2:])
        tzhour, tzmin = [(-2 * (m['tz'][0] == '-') + 1) * x for x in (tzhour, tzmin)]
        delta = datetime.timedelta(0,0,0,0, tzmin, tzhour)
    else:
        tzinfo = {
                 ('ut','gmt','z'): 0,
                 ('edt',): -4,
                 ('est','cdt'): -5,
                 ('cst','mdt'): -6,
                 ('mst','pdt'): -7,
                 ('pst',): -8,
                 ('a',): -1,
                 ('n',): 1,
                 ('m',): -12,
                 ('y',): 12,
                 }
        tzhour = (v for k, v in tzinfo.items() if m['tz'] in k).next()
        delta = datetime.timedelta(0,0,0,0,0, tzhour)
    stamp = datetime.datetime(*[m[x] for x in ('year','month','day','hour','minute','second')])
    return stamp - delta

class SuperDict(dict):
    """
    SuperDict is a dictionary object with keys posing as instance attributes.

    >>> i = SuperDict()
    >>> i.one = 1
    >>> i
    {'one': 1}
    """

    def __getattribute__(self, name):
        if dict.has_key(self, name):
            return dict.get(self, name)
        else:
            return dict.__getattribute__(self, name)

    def __setattr__(self, name, value):
        self[name] = value
        return value
