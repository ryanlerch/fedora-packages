# This file is part of Fedora Community.
# Copyright (C) 2008-2010  Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import ssl
import time

from datetime import datetime, timedelta
from pylons import config
from bugzilla import RHBugzilla3 as Bugzilla

from fedoracommunity.connectors.api import IConnector, ICall, IQuery, ParamFilter
from moksha.common.lib.dates import DateTimeDisplay

# Don't query closed bugs for these packages, since the queries timeout
BLACKLIST = ['kernel']

MAX_BZ_QUERIES = 200
BUG_SORT_KEYS = ['status', 'product', 'version', 'bug_id']

def chunks(l, n):
    """ Yield successive n-sized chunks from l. """
    for i in xrange(0, len(l), n):
        yield l[i:i+n]


class BugzillaConnector(IConnector, ICall, IQuery):
    _method_paths = {}
    _query_paths = {}

    def __init__(self, environ=None, request=None):
        super(BugzillaConnector, self).__init__(environ, request)
        self.__bugzilla = None

    @property
    def _bugzilla(self):
        """ A singleton over our python-bugzilla connection. """
        if not self.__bugzilla:
            self.__bugzilla = Bugzilla(
                url=self._base_url,
            )
        return self.__bugzilla

    # IConnector
    @classmethod
    def register(cls):
        cls._base_url = config.get('fedoracommunity.connector.bugzilla.baseurl',
                                   'https://bugzilla.redhat.com/xmlrpc.cgi')

        cls.register_query_bugs()

        path = cls.register_method('get_bug_stats', cls.query_bug_stats)

    #IQuery
    @classmethod
    def register_query_bugs(cls):
        path = cls.register_query(
                      'query_bugs',
                      cls.query_bugs,
                      primary_key_col='id',
                      default_sort_col='date',
                      default_sort_order=-1,
                      can_paginate=True)

        path.register_column('id',
                        default_visible=True,
                        can_sort=True,
                        can_filter_wildcards=False)

        path.register_column('status',
                        default_visible=True,
                        can_sort=True,
                        can_filter_wildcards=False)

        path.register_column('description',
                        default_visible=True,
                        can_sort=True,
                        can_filter_wildcards=False)

        path.register_column('release',
                        default_visible=True,
                        can_sort=True,
                        can_filter_wildcards=False)

        f = ParamFilter()
        f.add_filter('package', [], allow_none=False)
        f.add_filter('collection', [], allow_none=False)
        f.add_filter('version', [], allow_none=False)
        cls._query_bugs_filter = f

    def query_bug_stats(self, *args, **kw):
        package = kw.get('package', None)
        if not package:
            raise Exception('No package specified')
        bugzilla_cache = self._request.environ['beaker.cache'].get_cache('bugzilla')
        return bugzilla_cache.get_value(key=package, expiretime=21600,
                           createfunc=lambda: self._get_bug_stats(package))

    def _get_bug_stats(self, package, collection='Fedora'):
        """
        Returns (# of open bugs, # of new bugs, # of closed bugs)
        """
        queries = ['open', 'new', 'new_this_week', 'closed', 'closed_this_week']

        last_week = str(datetime.utcnow() - timedelta(days=7)),

        # Multi-call support is broken in the latest Bugzilla upgrade
        #mc = self._bugzilla._multicall()

        results = []

        # Open bugs
        if package in BLACKLIST:
            queries.remove('open')
        else:
            results.append(self._bugzilla.query({
                'product': collection,
                'component': package,
                'status': ['NEW', 'ASSIGNED', 'REOPENED'],
                }))

        # New bugs
        if package in BLACKLIST:
            queries.remove('new')
        else:
            results.append(self._bugzilla.query({
                'product': collection,
                'component': package,
                'status': 'NEW',
                }))

        # New bugs this week
        results.append(self._bugzilla.query({
            'product': collection,
            'component': package,
            'status': 'NEW',
            'creation_time': last_week,
            }))

        # Closed bugs
        if package in BLACKLIST:
            queries.remove('closed')
        else:
            results.append(self._bugzilla.query({
                'product': collection,
                'component': package,
                'status': 'CLOSED',
                }))

        # Closed bugs this week
        results.append(self._bugzilla.query({
            'product': collection,
            'component': package,
            'status': 'CLOSED',
            'creation_time': last_week,
            }))

        #results = dict([(q, len(r['bugs'])) for q, r in zip(queries, mc.run())])
        results = dict([(q, len(r)) for q, r in zip(queries, results)])

        return dict(results=results)

    def _is_security_bug(self, bug):
        security = False
        if bug.assigned_to == 'security-response-team@redhat.com':
            security = True
        elif bug.component == 'vulnerability':
            security = True
        elif 'Security' in bug.keywords:
            security = True
        elif bug.alias:
            for alias in bug.alias:
                if alias.startswith('CVE'):
                    security = True
                    break
        return security

    def query_bugs(self, start_row=None, rows_per_page=10, order=-1,
                   sort_col='number', filters=None, **params):
        if not filters:
            filters = {}

        filters = self._query_bugs_filter.filter(filters, conn=self)
        collection = filters.get('collection', 'Fedora')
        version = filters.get('version', '')

        package = filters['package']
        query = {
                'product': collection,
                'version': version,
                'component': package,
                'bug_status': [
                    'ASSIGNED', 'NEW', 'MODIFIED',
                    'ON_DEV', 'ON_QA', 'VERIFIED', 'FAILS_QA',
                    'RELEASE_PENDING', 'POST', 'REOPENED',
                ],
                #'order': 'bug_id',
                }

        bugzilla_cache = self._request.environ['beaker.cache'].get_cache('bugzilla')
        key = '%s_%s_%s' % (collection, version, package)
        bugs = bugzilla_cache.get_value(key=key, expiretime=900,
                createfunc=lambda: self._query_bugs(query,
                    filters=filters, collection=collection, **params))
        total_count = len(bugs)

        # This caching is a bit too aggressive
        #five_pages = rows_per_page * 5
        #if start_row < five_pages: # Cache the first 5 pages of every bug grid
        #    bugs = bugs[:five_pages]
        #    bugs = bugzilla_cache.get_value(key=key + '_details',
        #            expiretime=900, createfunc=lambda: self.get_bugs(
        #                bugs, collection=collection))

        # Sort based on feedback from users of bugz.fedoraproject.org/{package}
        # See https://fedorahosted.org/fedoracommunity/ticket/381
        bugs.sort(cmp=bug_sort)

        bugs = bugs[start_row:start_row+rows_per_page]
        #if start_row >= five_pages:
        bugs = self.get_bugs(bugs, collection=collection)
        return (total_count, bugs)

    def _query_bugs(self, query, start_row=None, rows_per_page=10, order=-1,
                   sort_col='number', filters=None, collection='Fedora',
                   **params):
        """ Make bugzilla queries but only grab up to 200 bugs at a time,
        otherwise we might drop due to SSL timeout.  :/
        """

        results, _results = [], None
        offset, limit = 0, MAX_BZ_QUERIES

        # XXX - This is a hack until the multicall stuff gets worked out
        # https://bugzilla.redhat.com/show_bug.cgi?id=824241 -- threebean
        while _results == None or len(_results):
            query.update(dict(offset=offset, limit=limit))
            _results = self._bugzilla.query(query)
            results.extend(_results)
            offset += limit

        return [
            dict(((key, getattr(bug, key)) for key in BUG_SORT_KEYS))
            for bug in results
        ]

    def get_bugs(self, bugids, collection='Fedora'):
        bugs = []

        # XXX - This is a hack until the multicall stuff gets worked out
        # https://bugzilla.redhat.com/show_bug.cgi?id=824241 -- threebean
        for chunk in chunks(bugids, 20):
            bugs.extend(self._bugzilla.getbugs([b['bug_id'] for b in chunk]))

        bugs_list = []
        for bug in bugs:
            modified = DateTimeDisplay(str(bug.last_change_time),
                                       format='%Y%m%dT%H:%M:%S')
            bug_class = ''
            if self._is_security_bug(bug):
                bug_class += 'security-bug '
            bugs_list.append({
                'id': bug.bug_id,
                'status': bug.bug_status.title(),
                'description': bug.summary,
                'last_modified': modified.age(),
                'release': '%s %s' % (collection, bug.version[0]),
                'bug_class': bug_class.strip(),
                })
        return bugs_list


def bug_sort(arg1, arg2):
    """ Sort bugs using logic adapted from old pkgdb.

    :author: Ralph Bean <rbean@redhat.com>

    """

    LARGE = 10000

    for key in BUG_SORT_KEYS:
        val1, val2 = arg1[key], arg2[key]

        if key == 'version':
            # version is a string which may contain an integer such as 13 or
            # a string such as 'rawhide'.  We want the integers first in
            # decending order followed by the strings.
            def version_to_int(val):
                try:
                    return -1 * int(val[0])
                except (ValueError, IndexError):
                    return -1 * LARGE

            val1, val2 = version_to_int(val1), version_to_int(val2)
        elif key == 'status':
            # We want items to appear by status in a certain order, not
            # alphabetically.  Items I forgot to hardcode just go last.
            status_order = ['NEW', 'ASSIGNED', 'MODIFIED', 'ON_QA', 'POST']
            def status_to_index(val):
                try:
                    return status_order.index(val)
                except ValueError, e:
                    return len(status_order)

            val1, val2 = status_to_index(val1), status_to_index(val2)

        result = cmp(val1, val2)
        if result:
            return result

    return 0
