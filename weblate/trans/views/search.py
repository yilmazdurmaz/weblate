# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext, ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from weblate.lang.models import Language
from weblate.trans.bulk import bulk_perform
from weblate.trans.forms import (
    BulkEditForm,
    ReplaceConfirmForm,
    ReplaceForm,
    SearchForm,
)
from weblate.trans.models import Change, Unit
from weblate.trans.util import render
from weblate.utils import messages
from weblate.utils.ratelimit import check_rate_limit
from weblate.utils.stats import ProjectLanguage
from weblate.utils.views import (
    get_component,
    get_paginator,
    get_project,
    get_sort_name,
    get_translation,
    import_message,
    show_form_errors,
)


def parse_url(request, project, component=None, lang=None):
    context = {"components": None}
    if component is None:
        if lang is None:
            obj = get_project(request, project)
            unit_set = Unit.objects.filter(translation__component__project=obj)
            context["project"] = obj
        else:
            project = get_project(request, project)
            language = get_object_or_404(Language, code=lang)
            obj = ProjectLanguage(project, language)
            unit_set = Unit.objects.filter(
                translation__component__project=project, translation__language=language
            )
            context["project"] = project
            context["language"] = language
    elif lang is None:
        obj = get_component(request, project, component)
        unit_set = Unit.objects.filter(translation__component=obj)
        context["component"] = obj
        context["project"] = obj.project
        context["components"] = [obj]
    else:
        obj = get_translation(request, project, component, lang)
        unit_set = obj.unit_set.all()
        context["translation"] = obj
        context["component"] = obj.component
        context["project"] = obj.component.project
        context["components"] = [obj.component]

    if not request.user.has_perm("unit.edit", obj):
        raise PermissionDenied

    return obj, unit_set, context


@login_required
@require_POST
def search_replace(request, project, component=None, lang=None):
    obj, unit_set, context = parse_url(request, project, component, lang)

    form = ReplaceForm(request.POST)

    if not form.is_valid():
        messages.error(request, gettext("Failed to process form!"))
        show_form_errors(request, form)
        return redirect(obj)

    search_text = form.cleaned_data["search"]
    replacement = form.cleaned_data["replacement"]
    query = form.cleaned_data.get("q")

    matching = unit_set.filter(target__contains=search_text)
    if query:
        matching = matching.search(query)

    updated = 0

    matching_ids = list(matching.order_by("id").values_list("id", flat=True)[:251])

    if matching_ids:
        if len(matching_ids) == 251:
            matching_ids = matching_ids[:250]
            limited = True

        matching = Unit.objects.filter(id__in=matching_ids).prefetch()

        confirm = ReplaceConfirmForm(matching, request.POST)
        limited = False

        if not confirm.is_valid():
            for unit in matching:
                unit.replacement = unit.target.replace(search_text, replacement)
            context.update(
                {
                    "matching": matching,
                    "search_query": search_text,
                    "replacement": replacement,
                    "form": form,
                    "limited": limited,
                    "confirm": ReplaceConfirmForm(matching),
                }
            )
            return render(request, "replace.html", context)

        matching = confirm.cleaned_data["units"]

        with transaction.atomic():
            for unit in matching.select_for_update():
                if not request.user.has_perm("unit.edit", unit):
                    continue
                unit.translate(
                    request.user,
                    unit.target.replace(search_text, replacement),
                    unit.state,
                    change_action=Change.ACTION_REPLACE,
                )
                updated += 1

    import_message(
        request,
        updated,
        gettext("Search and replace completed, no strings were updated."),
        ngettext(
            "Search and replace completed, %d string was updated.",
            "Search and replace completed, %d strings were updated.",
            updated,
        ),
    )

    return redirect(obj)


@never_cache
def search(request, project=None, component=None, lang=None):
    """Perform site-wide search on units."""
    is_ratelimited = not check_rate_limit("search", request)
    search_form = SearchForm(user=request.user, data=request.GET)
    sort = get_sort_name(request)
    context = {"search_form": search_form}
    if component:
        obj = get_component(request, project, component)
        context["component"] = obj
        context["project"] = obj.project
        context["component"] = obj
        context["back_url"] = obj.get_absolute_url()
    elif project:
        obj = get_project(request, project)
        context["project"] = obj
        context["back_url"] = obj.get_absolute_url()
    else:
        obj = None
        context["back_url"] = None
    if lang:
        s_language = get_object_or_404(Language, code=lang)
        context["language"] = s_language
        if obj:
            if component:
                context["back_url"] = obj.translation_set.get(
                    language=s_language
                ).get_absolute_url()
            else:
                context["back_url"] = reverse(
                    "project-language", kwargs={"project": project, "lang": lang}
                )
        else:
            context["back_url"] = s_language.get_absolute_url()

    if not is_ratelimited and request.GET and search_form.is_valid():
        # This is ugly way to hide query builder when showing results
        search_form = SearchForm(
            user=request.user, data=request.GET, show_builder=False
        )
        search_form.is_valid()
        # Filter results by ACL
        units = Unit.objects.prefetch_full().prefetch()
        if component:
            units = units.filter(translation__component=obj)
        elif project:
            units = units.filter(translation__component__project=obj)
        else:
            units = units.filter_access(request.user)
        units = units.search(
            search_form.cleaned_data.get("q", ""), project=context.get("project")
        )
        if lang:
            units = units.filter(translation__language=context["language"])

        units = get_paginator(
            request, units.order_by_request(search_form.cleaned_data, obj)
        )
        # Rebuild context from scratch here to get new form
        context.update(
            {
                "search_form": search_form,
                "show_results": True,
                "page_obj": units,
                "title": gettext("Search for %s") % (search_form.cleaned_data["q"]),
                "query_string": search_form.urlencode(),
                "search_url": search_form.urlencode(),
                "search_query": search_form.cleaned_data["q"],
                "search_items": search_form.items(),
                "filter_name": search_form.get_name(),
                "sort_name": sort["name"],
                "sort_query": sort["query"],
            }
        )
    elif is_ratelimited:
        messages.error(
            request, gettext("Too many search queries, please try again later.")
        )
    elif request.GET:
        messages.error(request, gettext("Invalid search query!"))
        show_form_errors(request, search_form)

    return render(request, "search.html", context)


@login_required
@require_POST
@never_cache
def bulk_edit(request, project, component=None, lang=None):
    obj, unit_set, context = parse_url(request, project, component, lang)

    if not request.user.has_perm("translation.auto", obj):
        raise PermissionDenied

    form = BulkEditForm(request.user, obj, request.POST, project=context["project"])

    if not form.is_valid():
        messages.error(request, gettext("Failed to process form!"))
        show_form_errors(request, form)
        return redirect(obj)

    updated = bulk_perform(
        request.user,
        unit_set,
        query=form.cleaned_data["q"],
        target_state=form.cleaned_data["state"],
        add_flags=form.cleaned_data["add_flags"],
        remove_flags=form.cleaned_data["remove_flags"],
        add_labels=form.cleaned_data["add_labels"],
        remove_labels=form.cleaned_data["remove_labels"],
        project=context["project"],
        components=context["components"],
    )

    import_message(
        request,
        updated,
        gettext("Bulk edit completed, no strings were updated."),
        ngettext(
            "Bulk edit completed, %d string was updated.",
            "Bulk edit completed, %d strings were updated.",
            updated,
        ),
    )

    return redirect(obj)
