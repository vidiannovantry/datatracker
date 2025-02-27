# Copyright The IETF Trust 2009-2022, All Rights Reserved
# -*- coding: utf-8 -*-
#
# Some parts Copyright (C) 2009-2010 Nokia Corporation and/or its subsidiary(-ies).
# All rights reserved. Contact: Pasi Eronen <pasi.eronen@nokia.com>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#
#  * Neither the name of the Nokia Corporation and/or its
#    subsidiary(-ies) nor the names of its contributors may be used
#    to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import re
import datetime

from collections import defaultdict

from django import forms
from django.conf import settings
from django.core.cache import cache, caches
from django.urls import reverse as urlreverse
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest, HttpResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import render
from django.utils import timezone
from django.utils.cache import _generate_cache_key # type: ignore



import debug                            # pyflakes:ignore

from ietf.doc.models import ( Document, DocHistory, DocAlias, State,
    LastCallDocEvent, NewRevisionDocEvent, IESG_SUBSTATE_TAGS,
    IESG_BALLOT_ACTIVE_STATES, IESG_STATCHG_CONFLREV_ACTIVE_STATES,
    IESG_CHARTER_ACTIVE_STATES )
from ietf.doc.fields import select2_id_doc_name_json
from ietf.doc.utils import get_search_cache_key, augment_events_with_revision, needed_ballot_positions
from ietf.group.models import Group
from ietf.idindex.index import active_drafts_index_by_group
from ietf.name.models import DocTagName, DocTypeName, StreamName
from ietf.person.models import Person
from ietf.person.utils import get_active_ads
from ietf.utils.draft_search import normalize_draftname
from ietf.utils.log import log
from ietf.doc.utils_search import prepare_document_table


class SearchForm(forms.Form):
    name = forms.CharField(required=False)
    rfcs = forms.BooleanField(required=False, initial=True)
    activedrafts = forms.BooleanField(required=False, initial=True)
    olddrafts = forms.BooleanField(required=False, initial=False)

    by = forms.ChoiceField(choices=[(x,x) for x in ('author','group','area','ad','state','irtfstate','stream')], required=False, initial='group')
    author = forms.CharField(required=False)
    group = forms.CharField(required=False)
    stream = forms.ModelChoiceField(StreamName.objects.all().order_by('name'), empty_label="any stream", required=False)
    area = forms.ModelChoiceField(Group.objects.filter(type="area", state="active").order_by('name'), empty_label="any area", required=False)
    ad = forms.ChoiceField(choices=(), required=False)
    state = forms.ModelChoiceField(State.objects.filter(type="draft-iesg"), empty_label="any state", required=False)
    substate = forms.ChoiceField(choices=(), required=False)
    irtfstate = forms.ModelChoiceField(State.objects.filter(type="draft-stream-irtf"), empty_label="any state", required=False)

    sort = forms.ChoiceField(
        choices= (
            ("document", "Document"), ("-document", "Document (desc.)"),
            ("title", "Title"), ("-title", "Title (desc.)"),
            ("date", "Date"), ("-date", "Date (desc.)"),
            ("status", "Status"), ("-status", "Status (desc.)"),
            ("ipr", "Ipr"), ("ipr", "Ipr (desc.)"),
            ("ad", "AD"), ("-ad", "AD (desc)"), ),
        required=False, widget=forms.HiddenInput)

    doctypes = forms.ModelMultipleChoiceField(queryset=DocTypeName.objects.filter(used=True).exclude(slug__in=('draft','liai-att')).order_by('name'), required=False)

    def __init__(self, *args, **kwargs):
        super(SearchForm, self).__init__(*args, **kwargs)
        responsible = Document.objects.values_list('ad', flat=True).distinct()
        active_ads = get_active_ads()
        inactive_ads = list(((Person.objects.filter(pk__in=responsible) | Person.objects.filter(role__name="pre-ad",
                                                                                              role__group__type="area",
                                                                                              role__group__state="active")).distinct())
                            .exclude(pk__in=[x.pk for x in active_ads]))
        extract_last_name = lambda x: x.name_parts()[3]
        active_ads.sort(key=extract_last_name)
        inactive_ads.sort(key=extract_last_name)

        self.fields['ad'].choices = [('', 'any AD')] + [(ad.pk, ad.plain_name()) for ad in active_ads] + [('', '------------------')] + [(ad.pk, ad.name) for ad in inactive_ads]
        self.fields['substate'].choices = [('', 'any substate'), ('0', 'no substate')] + [(n.slug, n.name) for n in DocTagName.objects.filter(slug__in=IESG_SUBSTATE_TAGS)]

    def clean_name(self):
        value = self.cleaned_data.get('name','')
        return normalize_draftname(value)

    def clean(self):
        q = self.cleaned_data
        # Reset query['by'] if needed
        if 'by' in q:
            for k in ('author', 'group', 'area', 'ad'):
                if q['by'] == k and not q.get(k):
                    q['by'] = None
            if q['by'] == 'state' and not (q.get('state') or q.get('substate')):
                q['by'] = None
            if q['by'] == 'irtfstate' and not (q.get('irtfstate')):
                q['by'] = None
        else:
            q['by'] = None
        # Reset other fields
        for k in ('author','group', 'area', 'ad'):
            if k != q['by']:
                q[k] = ""
        if q['by'] != 'state':
            q['state'] = q['substate'] = None
        if q['by'] != 'irtfstate':
            q['irtfstate'] = None
        return q

def retrieve_search_results(form, all_types=False):
    """Takes a validated SearchForm and return the results."""

    if not form.is_valid():
        raise ValueError("SearchForm doesn't validate: %s" % form.errors)

    query = form.cleaned_data

    if all_types:
        # order by time here to retain the most recent documents in case we
        # find too many and have to chop the results list
        docs = Document.objects.all().order_by('-time')
    else:
        types = []

        if query['activedrafts'] or query['olddrafts'] or query['rfcs']:
            types.append('draft')

        types.extend(query["doctypes"])

        if not types:
            return Document.objects.none()

        docs = Document.objects.filter(type__in=types)

    # name
    if query["name"]:
        docs = docs.filter(Q(docalias__name__icontains=query["name"]) |
                           Q(title__icontains=query["name"])).distinct()

    # rfc/active/old check buttons
    allowed_draft_states = []
    if query["rfcs"]:
        allowed_draft_states.append("rfc")
    if query["activedrafts"]:
        allowed_draft_states.append("active")
    if query["olddrafts"]:
        allowed_draft_states.extend(['repl', 'expired', 'auth-rm', 'ietf-rm'])

    docs = docs.filter(Q(states__slug__in=allowed_draft_states) |
                       ~Q(type__slug='draft')).distinct()

    # radio choices
    by = query["by"]
    if by == "author":
        docs = docs.filter(
            Q(documentauthor__person__alias__name__icontains=query["author"]) |
            Q(documentauthor__person__email__address__icontains=query["author"])
        )
    elif by == "group":
        docs = docs.filter(group__acronym__iexact=query["group"])
    elif by == "area":
        docs = docs.filter(Q(group__type="wg", group__parent=query["area"]) |
                           Q(group=query["area"])).distinct()
    elif by == "ad":
        docs = docs.filter(ad=query["ad"])
    elif by == "state":
        if query["state"]:
            docs = docs.filter(states=query["state"])
        if query["substate"]:
            docs = docs.filter(tags=query["substate"])
    elif by == "irtfstate":
        docs = docs.filter(states=query["irtfstate"])
    elif by == "stream":
        docs = docs.filter(stream=query["stream"])

    return docs

def search(request):
    if request.GET:
        # backwards compatibility
        get_params = request.GET.copy()
        if 'activeDrafts' in request.GET:
            get_params['activedrafts'] = request.GET['activeDrafts']
        if 'oldDrafts' in request.GET:
            get_params['olddrafts'] = request.GET['oldDrafts']
        if 'subState' in request.GET:
            get_params['substate'] = request.GET['subState']

        form = SearchForm(get_params)
        if not form.is_valid():
            return HttpResponseBadRequest("form not valid: %s" % form.errors)

        cache_key = get_search_cache_key(get_params)
        cached_val = cache.get(cache_key)
        if cached_val:
            [results, meta] = cached_val
        else:
            results = retrieve_search_results(form)
            results, meta = prepare_document_table(request, results, get_params)
            cache.set(cache_key, [results, meta]) # for settings.CACHE_MIDDLEWARE_SECONDS
            log(f"Search results computed for {get_params}")
        meta['searching'] = True
    else:
        form = SearchForm()
        results = []
        meta = { 'by': None, 'searching': False }
        get_params = QueryDict('')

    return render(request, 'doc/search/search.html', {
        'form':form, 'docs':results, 'meta':meta, 'queryargs':get_params.urlencode() },
    )

def frontpage(request):
    form = SearchForm()
    return render(request, 'doc/frontpage.html', {'form':form})

def search_for_name(request, name):
    def find_unique(n):
        exact = DocAlias.objects.filter(name__iexact=n).first()
        if exact:
            return exact.name

        aliases = DocAlias.objects.filter(name__istartswith=n)[:2]
        if len(aliases) == 1:
            return aliases[0].name

        aliases = DocAlias.objects.filter(name__icontains=n)[:2]
        if len(aliases) == 1:
            return aliases[0].name

        return None

    def cached_redirect(cache_key, url):
        cache.set(cache_key, url, settings.CACHE_MIDDLEWARE_SECONDS)
        return HttpResponseRedirect(url)

    n = name

    cache_key = _generate_cache_key(request, 'GET', [], settings.CACHE_MIDDLEWARE_KEY_PREFIX)
    if cache_key:
        url = cache.get(cache_key, None)
        if url:
            return HttpResponseRedirect(url)

    # chop away extension
    extension_split = re.search(r"^(.+)\.(txt|ps|pdf)$", n)
    if extension_split:
        n = extension_split.group(1)

    redirect_to = find_unique(name)
    if redirect_to:
        return cached_redirect(cache_key, urlreverse("ietf.doc.views_doc.document_main", kwargs={ "name": redirect_to }))
    else:
        # check for embedded rev - this may be ambiguous, so don't
        # chop it off if we don't find a match
        rev_split = re.search("^(.+)-([0-9]{2})$", n)
        if rev_split:
            redirect_to = find_unique(rev_split.group(1))
            if redirect_to:
                rev = rev_split.group(2)
                # check if we can redirect directly to the rev if it's draft, if rfc - always redirect to main page
                if not redirect_to.startswith('rfc') and DocHistory.objects.filter(doc__docalias__name=redirect_to, rev=rev).exists():
                    return cached_redirect(cache_key, urlreverse("ietf.doc.views_doc.document_main", kwargs={ "name": redirect_to, "rev": rev }))
                else:
                    return cached_redirect(cache_key, urlreverse("ietf.doc.views_doc.document_main", kwargs={ "name": redirect_to }))

    # build appropriate flags based on string prefix
    doctypenames = DocTypeName.objects.filter(used=True)
    # This would have been more straightforward if document prefixes couldn't
    # contain a dash.  Probably, document prefixes shouldn't contain a dash ...
    search_args = "?name=%s" % n
    if   n.startswith("draft"):
        search_args += "&rfcs=on&activedrafts=on&olddrafts=on"
    else:
        for t in doctypenames:
            if t.prefix and n.startswith(t.prefix):
                search_args += "&doctypes=%s" % t.slug
                break
        else:
            search_args += "&rfcs=on&activedrafts=on&olddrafts=on"

    return cached_redirect(cache_key, urlreverse('ietf.doc.views_search.search') + search_args)

def ad_dashboard_group_type(doc):
    # Return group type for document for dashboard.
    # If doc is not defined return list of all possible
    # group types
    if not doc:
        return ('I-D', 'RFC', 'Conflict Review', 'Status Change', 'Charter')
    if doc.type.slug=='draft':
        if doc.get_state_slug('draft') == 'rfc':
            return 'RFC'
        elif doc.get_state_slug('draft') == 'active' and doc.get_state_slug('draft-iesg') and doc.get_state('draft-iesg').name =='RFC Ed Queue':
            return 'RFC'
        elif doc.get_state_slug('draft') == 'active' and doc.get_state_slug('draft-iesg') and doc.get_state('draft-iesg').name in ('Dead', 'I-D Exists', 'AD is watching'):
             return None
        elif doc.get_state('draft').name in ('Expired', 'Replaced'):
            return None
        else:
            return 'I-D'
    elif doc.type.slug=='conflrev':
          return 'Conflict Review'
    elif doc.type.slug=='statchg':
        return 'Status Change'
    elif doc.type.slug=='charter':
        return "Charter"
    else:
        return "Document"

def ad_dashboard_group(doc):

    if doc.type.slug=='draft':
        if doc.get_state_slug('draft') == 'rfc':
            return 'RFC'
        elif doc.get_state_slug('draft') == 'active' and doc.get_state_slug('draft-iesg'):
            return '%s Internet-Draft' % doc.get_state('draft-iesg').name
        else:
            return '%s Internet-Draft' % doc.get_state('draft').name
    elif doc.type.slug=='conflrev':
        if doc.get_state_slug('conflrev') in ('appr-reqnopub-sent','appr-noprob-sent'):
            return 'Approved Conflict Review'
        elif doc.get_state_slug('conflrev') in ('appr-reqnopub-pend','appr-noprob-pend','appr-reqnopub-pr','appr-noprob-pr'):
            return "%s Conflict Review" % State.objects.get(type__slug='draft-iesg',slug='approved')
        else:
          return '%s Conflict Review' % doc.get_state('conflrev')
    elif doc.type.slug=='statchg':
        if doc.get_state_slug('statchg') in ('appr-sent',):
            return 'Approved Status Change'
        if doc.get_state_slug('statchg') in ('appr-pend','appr-pr'):
            return '%s Status Change' % State.objects.get(type__slug='draft-iesg',slug='approved')
        else:
            return '%s Status Change' % doc.get_state('statchg')
    elif doc.type.slug=='charter':
        if doc.get_state_slug('charter') == 'approved':
            return "Approved Charter"
        else:
            return '%s Charter' % doc.get_state('charter')
    else:
        return "Document"


def shorten_group_name(name):
    for s in [
        " Internet-Draft",
        " Conflict Review",
        " Status Change",
        " (Internal Steering Group/IAB Review) Charter",
        "Charter",
    ]:
        if name.endswith(s):
            name = name[: -len(s)]

    for pat, sub in [
        ("Writeup", "Write-up"),
        ("Requested", "Req"),
        ("Evaluation", "Eval"),
        ("Publication", "Pub"),
        ("Waiting", "Wait"),
        ("Go-Ahead", "OK"),
        ("Approved-", "App, "),
        ("announcement", "ann."),
        ("IESG Eval - ", ""),
        ("Not currently under review", "Not under review"),
        ("External Review", "Ext. Review"),
        (r"IESG Review \(Charter for Approval, Selected by Secretariat\)", "IESG Review"),
        ("Needs Shepherd", "Needs Shep."),
        ("Approved", "App."),
        ("Replaced", "Repl."),
        ("Withdrawn", "Withd."),
        ("Chartering/Rechartering", "Charter"),
        (r"\(Message to Community, Selected by Secretariat\)", "")
    ]:
        name = re.sub(pat, sub, name)

    return name.strip()


def ad_dashboard_sort_key(doc):

    if doc.type.slug=='draft' and doc.get_state_slug('draft') == 'rfc':
        return "21%04d" % int(doc.rfc_number())
    if doc.type.slug=='statchg' and doc.get_state_slug('statchg') == 'appr-sent':
        return "22%d" % 0 # TODO - get the date of the transition into this state here
    if doc.type.slug=='conflrev' and doc.get_state_slug('conflrev') in ('appr-reqnopub-sent','appr-noprob-sent'):
        return "23%d" % 0 # TODO - get the date of the transition into this state here
    if doc.type.slug=='charter' and doc.get_state_slug('charter') == 'approved':
        return "24%d" % 0 # TODO - get the date of the transition into this state here

    seed = ad_dashboard_group(doc)

    if doc.type.slug=='conflrev' and doc.get_state_slug('conflrev') == 'adrev':
        state = State.objects.get(type__slug='draft-iesg',slug='ad-eval')
        return "1%d%s" % (state.order,seed)

    if doc.type.slug=='charter' and doc.get_state_slug('charter') != 'replaced':
        if doc.get_state_slug('charter') in ('notrev','infrev'):
            return "100%s" % seed
        elif  doc.get_state_slug('charter') == 'intrev':
            state = State.objects.get(type__slug='draft-iesg',slug='ad-eval')
            return "1%d%s" % (state.order,seed)
        elif  doc.get_state_slug('charter') == 'extrev':
            state = State.objects.get(type__slug='draft-iesg',slug='lc')
            return "1%d%s" % (state.order,seed)
        elif  doc.get_state_slug('charter') == 'iesgrev':
            state = State.objects.get(type__slug='draft-iesg',slug='iesg-eva')
            return "1%d%s" % (state.order,seed)

    if doc.type.slug=='statchg' and  doc.get_state_slug('statchg') == 'adrev':
        state = State.objects.get(type__slug='draft-iesg',slug='ad-eval')
        return "1%d%s" % (state.order,seed)

    if seed.startswith('Needs Shepherd'):
        return "100%s" % seed
    if seed.endswith(' Document'):
        seed = seed[:-9]
    elif seed.endswith(' Internet-Draft'):
        seed = seed[:-15]
    elif seed.endswith(' Conflict Review'):
        seed = seed[:-16]
    elif seed.endswith(' Status Change'):
        seed = seed[:-14]
    state = State.objects.filter(type__slug='draft-iesg',name=seed)
    if state:
        ageseconds = 0
        changetime= doc.latest_event(type='changed_document')
        if changetime:
            ad = (timezone.now()-doc.latest_event(type='changed_document').time)
            ageseconds = (ad.microseconds + (ad.seconds + ad.days * 24 * 3600) * 10**6) / 10**6
        return "1%d%s%s%010d" % (state[0].order,seed,doc.type.slug,ageseconds)

    return "3%s" % seed


def ad_workload(request):
    delta = datetime.timedelta(days=120)
    right_now = timezone.now()

    ads = []
    responsible = Document.objects.values_list("ad", flat=True).distinct()
    for p in Person.objects.filter(
        Q(
            role__name__in=("pre-ad", "ad"),
            role__group__type="area",
            role__group__state="active",
        )
        | Q(pk__in=responsible)
    ).distinct():
        if p in get_active_ads():
            ads.append(p)

    doctypes = list(
        DocTypeName.objects.filter(used=True)
        .exclude(slug__in=("draft", "liai-att"))
        .values_list("pk", flat=True)
    )

    up_is_good = {}
    group_types = ad_dashboard_group_type(None)
    groups = {g: {} for g in group_types}
    group_names = {g: [] for g in group_types}

    # Prefill groups in preferred sort order
    # FIXME: This should really use the database states instead of replicating the logic
    for id, (g, uig) in enumerate(
        [
            ("Publication Requested Internet-Draft", False),
            ("AD Evaluation Internet-Draft", False),
            ("Last Call Requested Internet-Draft", True),
            ("In Last Call Internet-Draft", True),
            ("Waiting for Writeup Internet-Draft", False),
            ("IESG Evaluation - Defer Internet-Draft", False),
            ("IESG Evaluation Internet-Draft", True),
            ("Waiting for AD Go-Ahead Internet-Draft", False),
            ("Approved-announcement to be sent Internet-Draft", True),
            ("Approved-announcement sent Internet-Draft", True),
        ]
    ):
        groups["I-D"][g] = id
        group_names["I-D"].append(g)
        up_is_good[g] = uig

    for id, g in enumerate(["RFC Ed Queue Internet-Draft", "RFC"]):
        groups["RFC"][g] = id
        group_names["RFC"].append(g)
        up_is_good[g] = True

    for id, (g, uig) in enumerate(
        [
            ("AD Review Conflict Review", False),
            ("Needs Shepherd Conflict Review", False),
            ("IESG Evaluation Conflict Review", True),
            ("Approved Conflict Review", True),
            ("Withdrawn Conflict Review", None),
        ]
    ):
        groups["Conflict Review"][g] = id
        group_names["Conflict Review"].append(g)
        up_is_good[g] = uig

    for id, (g, uig) in enumerate(
        [
            ("Publication Requested Status Change", False),
            ("AD Evaluation Status Change", False),
            ("Last Call Requested Status Change", True),
            ("In Last Call Status Change", True),
            ("Waiting for Writeup Status Change", False),
            ("IESG Evaluation Status Change", True),
            ("Waiting for AD Go-Ahead Status Change", False),
        ]
    ):
        groups["Status Change"][g] = id
        group_names["Status Change"].append(g)
        up_is_good[g] = uig

    for id, (g, uig) in enumerate(
        [
            ("Not currently under review Charter", None),
            ("Draft Charter Charter", None),
            ("Start Chartering/Rechartering (Internal Steering Group/IAB Review) Charter", False),
            ("External Review (Message to Community, Selected by Secretariat) Charter", True),
            ("IESG Review (Charter for Approval, Selected by Secretariat) Charter", True),
            ("Approved Charter", True),
            ("Replaced Charter", None),
        ]
    ):
        groups["Charter"][g] = id
        group_names["Charter"].append(g)
        up_is_good[g] = uig

    for ad in ads:
        form = SearchForm(
            {
                "by": "ad",
                "ad": ad.id,
                "rfcs": "on",
                "activedrafts": "on",
                "olddrafts": "on",
                "doctypes": doctypes,
            }
        )

        ad.dashboard = urlreverse(
            "ietf.doc.views_search.docs_for_ad", kwargs=dict(name=ad.full_name_as_key())
        )
        ad.counts = defaultdict(list)
        ad.prev = defaultdict(list)
        ad.doc_now = defaultdict(list)
        ad.doc_prev = defaultdict(list)

        for doc in retrieve_search_results(form):
            group_type = ad_dashboard_group_type(doc)
            if group_type and group_type in groups:
                # Right now, anything with group_type "Document", such as a bofreq is not handled.
                group = ad_dashboard_group(doc)
                if group not in groups[group_type]:
                    groups[group_type][group] = len(groups[group_type])
                    group_names[group_type].append(group)

                inc = len(groups[group_type]) - len(ad.counts[group_type])
                if inc > 0:
                    ad.counts[group_type].extend([0] * inc)
                    ad.prev[group_type].extend([0] * inc)
                    ad.doc_now[group_type].extend(set() for _ in range(inc))
                    ad.doc_prev[group_type].extend(set() for _ in range(inc))

                ad.counts[group_type][groups[group_type][group]] += 1
                ad.doc_now[group_type][groups[group_type][group]].add(doc)

                last_state_event = (
                    doc.docevent_set.filter(
                        Q(type="started_iesg_process") | Q(type="changed_state")
                    )
                    .order_by("-time")
                    .first()
                )
                if (last_state_event is not None) and (right_now - last_state_event.time) > delta:
                    ad.prev[group_type][groups[group_type][group]] += 1
                    ad.doc_prev[group_type][groups[group_type][group]].add(doc)

    for ad in ads:
        ad.doc_diff = defaultdict(list)
        for gt in group_types:
            inc = len(groups[gt]) - len(ad.counts[gt])
            if inc > 0:
                ad.counts[gt].extend([0] * inc)
                ad.prev[gt].extend([0] * inc)
                ad.doc_now[gt].extend([set()] * inc)
                ad.doc_prev[gt].extend([set()] * inc)

            ad.doc_diff[gt].extend([set()] * len(groups[gt]))
            for idx, g in enumerate(group_names[gt]):
                ad.doc_diff[gt][idx] = ad.doc_prev[gt][idx] ^ ad.doc_now[gt][idx]

    # Shorten the names of groups
    for gt in group_types:
        for idx, g in enumerate(group_names[gt]):
            group_names[gt][idx] = (
                shorten_group_name(g),
                g,
                up_is_good[g] if g in up_is_good else None,
            )

    workload = [
        dict(
            group_type=gt,
            group_names=group_names[gt],
            counts=[
                (
                    ad,
                    [
                        (
                            group_names[gt][index],
                            ad.counts[gt][index],
                            ad.prev[gt][index],
                            ad.doc_diff[gt][index],
                        )
                        for index in range(len(group_names[gt]))
                    ],
                )
                for ad in ads
            ],
            sums=[
                (
                    group_names[gt][index],
                    sum([ad.counts[gt][index] for ad in ads]),
                    sum([ad.prev[gt][index] for ad in ads]),
                )
                for index in range(len(group_names[gt]))
            ],
        )
        for gt in group_types
    ]

    return render(request, "doc/ad_list.html", {"workload": workload, "delta": delta})

def docs_for_ad(request, name):
    ad = None
    responsible = Document.objects.values_list('ad', flat=True).distinct()
    for p in Person.objects.filter(Q(role__name__in=("pre-ad", "ad"),
                                     role__group__type="area",
                                     role__group__state="active")
                                   | Q(pk__in=responsible)).distinct():
        if name == p.full_name_as_key():
            ad = p
            break
    if not ad:
        raise Http404
    form = SearchForm({'by':'ad','ad': ad.id,
                       'rfcs':'on', 'activedrafts':'on', 'olddrafts':'on',
                       'sort': 'status',
                       'doctypes': list(DocTypeName.objects.filter(used=True).exclude(slug__in=('draft','liai-att')).values_list("pk", flat=True))})
    results, meta = prepare_document_table(request, retrieve_search_results(form), form.data, max_results=500)
    results.sort(key=ad_dashboard_sort_key)
    del meta["headers"][-1]

    # filter out some results
    results = [
        r
        for r in results
        if not (
            r.type_id == "charter"
            and (
                r.group.state_id == "abandon"
                or r.get_state_slug("charter") == "replaced"
            )
        )
        and not (
            r.type_id == "draft"
            and (
                r.get_state_slug("draft-iesg") == "dead"
                or r.get_state_slug("draft") == "repl"
            )
        )
    ]

    for d in results:
        d.search_heading = ad_dashboard_group(d)

    # Additional content showing docs with blocking positions by this AD,
    # and docs that the AD hasn't balloted on that are lacking ballot positions to progress
    blocked_docs = []
    not_balloted_docs = []
    if ad in get_active_ads():
        iesg_docs = Document.objects.filter(Q(states__type="draft-iesg",
                                              states__slug__in=IESG_BALLOT_ACTIVE_STATES) |
                                            Q(states__type="charter",
                                              states__slug__in=IESG_CHARTER_ACTIVE_STATES) |
                                            Q(states__type__in=("statchg", "conflrev"),
                                              states__slug__in=IESG_STATCHG_CONFLREV_ACTIVE_STATES)).distinct()
        possible_docs = iesg_docs.filter(docevent__ballotpositiondocevent__pos__blocking=True,
                                         docevent__ballotpositiondocevent__balloter=ad)
        for doc in possible_docs:
            ballot = doc.active_ballot()
            if not ballot:
                continue

            blocking_positions = [p for p in ballot.all_positions() if p.pos.blocking]
            if not blocking_positions or not any( p.balloter==ad for p in blocking_positions ):
                continue

            augment_events_with_revision(doc, blocking_positions)

            doc.blocking_positions = blocking_positions
            doc.ballot = ballot

            blocked_docs.append(doc)

        # latest first
        if blocked_docs:
            blocked_docs.sort(key=lambda d: min(p.time for p in d.blocking_positions if p.balloter==ad), reverse=True)

        possible_docs = iesg_docs.exclude(
            Q(docevent__ballotpositiondocevent__balloter=ad)
        )
        for doc in possible_docs:
            ballot = doc.active_ballot()
            if (
                not ballot
                or doc.get_state_slug("draft") == "repl"
                or (doc.telechat_date() and doc.telechat_date() > timezone.now().date())
            ):
                continue

            iesg_ballot_summary = needed_ballot_positions(
                doc, list(ballot.active_balloter_positions().values())
            )
            if re.search(r"\bNeeds\s+\d+", iesg_ballot_summary):
                not_balloted_docs.append(doc)

    return render(request, 'doc/drafts_for_ad.html', {
        'form':form, 'docs':results, 'meta':meta, 'ad_name': ad.plain_name(), 'blocked_docs': blocked_docs, 'not_balloted_docs': not_balloted_docs
    })
def drafts_in_last_call(request):
    lc_state = State.objects.get(type="draft-iesg", slug="lc").pk
    form = SearchForm({'by':'state','state': lc_state, 'rfcs':'on', 'activedrafts':'on'})
    results, meta = prepare_document_table(request, retrieve_search_results(form), form.data)
    pages = 0
    for doc in results:
        pages += doc.pages

    return render(request, 'doc/drafts_in_last_call.html', {
        'form':form, 'docs':results, 'meta':meta, 'pages':pages
    })

def drafts_in_iesg_process(request):
    states = State.objects.filter(type="draft-iesg").exclude(slug__in=('idexists', 'pub', 'dead', 'watching', 'rfcqueue'))
    title = "Documents in IESG process"

    grouped_docs = []

    for s in states.order_by("order"):
        docs = Document.objects.filter(type="draft", states=s).distinct().order_by("time").select_related("ad", "group", "group__parent")
        if docs:
            if s.slug == "lc":
                for d in docs:
                    e = d.latest_event(LastCallDocEvent, type="sent_last_call")
                    # If we don't have an event, use an arbitrary date in the past (but not datetime.datetime.min,
                    # which causes problems with timezone conversions)
                    d.lc_expires = e.expires if e else datetime.datetime(1950, 1, 1)
                docs = list(docs)
                docs.sort(key=lambda d: d.lc_expires)

            grouped_docs.append((s, docs))

    return render(request, 'doc/drafts_in_iesg_process.html', {
            "grouped_docs": grouped_docs,
            "title": title,
            })

def recent_drafts(request, days=7):
    slowcache = caches['slowpages']
    cache_key = f'recentdraftsview{days}' 
    cached_val = slowcache.get(cache_key)
    if not cached_val:
        since = timezone.now()-datetime.timedelta(days=days)
        state = State.objects.get(type='draft', slug='active')
        events = NewRevisionDocEvent.objects.filter(time__gt=since)
        names = [ e.doc.name for e in events ]
        docs = Document.objects.filter(name__in=names, states=state)
        results, meta = prepare_document_table(request, docs, query={'sort':'-date', }, max_results=len(names))
        slowcache.set(cache_key, [docs, results, meta], 1800)
    else:
        [docs, results, meta] = cached_val

    pages = 0
    for doc in results:
        pages += doc.pages or 0

    return render(request, 'doc/recent_drafts.html', {
        'docs':results, 'meta':meta, 'pages':pages, 'days': days,
    })


def index_all_drafts(request):
    # try to be efficient since this view returns a lot of data
    categories = []

    for s in ("active", "rfc", "expired", "repl", "auth-rm", "ietf-rm"):
        state = State.objects.get(type="draft", slug=s)

        if state.slug == "rfc":
            heading = "RFCs"
        elif state.slug in ("ietf-rm", "auth-rm"):
            heading = "Internet-Drafts %s" % state.name
        else:
            heading = "%s Internet-Drafts" % state.name

        draft_names = DocAlias.objects.filter(docs__states=state).values_list("name", "docs__name")

        names = []
        names_to_skip = set()
        for name, doc in draft_names:
            sort_key = name
            if name != doc:
                if not name.startswith("rfc"):
                    name, doc = doc, name
                names_to_skip.add(doc)

            if name.startswith("rfc"):
                name = name.upper()
                sort_key = '%09d' % (100000000-int(name[3:]))

            names.append((name, sort_key))

        names.sort(key=lambda t: t[1])

        names = [f'<a href=\"{urlreverse("ietf.doc.views_doc.document_main", kwargs=dict(name=n))}\">{n}</a>'
                 for n, __ in names if n not in names_to_skip]

        categories.append((state,
                      heading,
                      len(names),
                      "<br>".join(names)
                      ))
    return render(request, 'doc/index_all_drafts.html', { "categories": categories })

def index_active_drafts(request):
    slowcache = caches['slowpages']
    cache_key = 'doc:index_active_drafts'
    groups = slowcache.get(cache_key)
    if not groups:
        groups = active_drafts_index_by_group()
        slowcache.set(cache_key, groups, 15*60)
    return render(request, "doc/index_active_drafts.html", { 'groups': groups })

def ajax_select2_search_docs(request, model_name, doc_type):
    if model_name == "docalias":
        model = DocAlias
    else:
        model = Document

    q = [w.strip() for w in request.GET.get('q', '').split() if w.strip()]

    if not q:
        objs = model.objects.none()
    else:
        qs = model.objects.all()

        if model == Document:
            qs = qs.filter(type=doc_type)
        elif model == DocAlias:
            qs = qs.filter(docs__type=doc_type)

        for t in q:
            qs = qs.filter(name__icontains=t)

        objs = qs.distinct().order_by("name")[:20]

    return HttpResponse(select2_id_doc_name_json(model, objs), content_type='application/json')
