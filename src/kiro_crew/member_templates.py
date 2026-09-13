"""Crew templates: the store listing a crew member is hired from.

A template is an installed app's Custom Agent plus the job card its manifest's
``crew`` section carries (:class:`kiro_crew.apps.manifest.CrewTemplate`). This
module is the read side the hire route composes:

* :func:`resolve_store_template` -- from ``{app, agent}`` to the materialized
  agent file (``<app>--<agent>``, the copy ``apps.bridges`` writes into the
  agents directory when the app is enabled), the card, the version, the agent
  definition and the initial briefing text.
* :func:`template_ref` -- the ``template`` the wrapper row records
  (``<app>/<agent>``), the same namespacing the materialized file uses.
* :func:`write_pristine_copy` / :func:`read_pristine_copy` -- the unmodified
  template payload at the installed version, kept at
  ``<data home>/member-templates/<member id>.json`` (a gateway-only top-level
  leaf: sandbox-masked, refused to agent file tools) and stamped with the member's id
  and private-store generation: the BASE of the role update's three-way merge
  (MINE = the member's agent file and card fields, THEIRS = the template as
  installed now).
* :func:`resolve_template_ref` -- from a wrapper row's ``template``
  (``<app>/<agent name>``) back to the listing, for the update.
* :func:`plan_role_update` / :func:`merge_role_update` -- the per-field
  three-way merge: only THEIRS changed -> apply, only MINE changed -> keep,
  both -> the user picks. Scope is the template-provided definition (the agent
  file's keys except ``name``, which is the member's id) plus the card's
  ``role`` and ``triggers`` -- never lived state.
* :func:`seed_briefing` -- write ``initial_briefing`` as the new member's own
  ``briefing.md``; from then on the file is the member's lived state and no
  template operation touches it.

Leaf-ish on purpose: imports the manifest, the members module and the agents
directory resolver, never the dashboard handlers.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import members, platform_compat
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.apps import admission as _admission
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.bridges import _namespace, _safe_link_name, render_app_agent_spec
from kiro_crew.apps.manager import _read_installed, app_dir, get_app_manifest
from kiro_crew.apps.manifest import CrewTemplate, _path_escapes_app_root
from kiro_crew.config.paths import data_home
from kiro_crew.pinned_fs import (
    create_and_open_dir_pinned,
    read_bytes_pinned,
    read_file_pinned,
    write_file_pinned,
)
from kiro_crew.sel import sel
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)

#: The pristine copies' directory: a TOP-LEVEL leaf of the data home holding one
#: ``<member id>.json`` per member, bind-masked from every sandboxed process
#: (``sandbox._CREW_HIDDEN_LEAVES``), pre-created before each spawn so the mask
#: has a name to bind over (``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``) and refused
#: to the agent file tools on every OS (``security._CREW_SECRET_LEAVES``). NOT
#: inside ``members/<slug>/``: that directory is agent-writable and its slug is
#: lossy (two ids can share one). NOT under ``trust/`` either: that subtree is
#: sandbox-VISIBLE (its SEL key and log have in-sandbox readers and appenders),
#: so a spawned interpreter's ``open()`` could rewrite a base there, bypassing
#: the file-tool gate. A forged or shared BASE makes the merge skip a template
#: change or overwrite a customization silently, which is the harm the fence
#: exists for; only the gateway reads or writes a base.
PRISTINE_COPIES_DIR_NAME = "member-templates"

#: Largest initial briefing a template may seed (bytes). The member's own
#: briefing is injection-capped downstream; this bounds what a store listing can
#: put on disk in one hire.
INITIAL_BRIEFING_MAX_BYTES = 64 * 1024

#: Largest agent definition a template may ship (bytes) -- the same order as the
#: spec reader caps elsewhere; a store listing is not a place for a novel.
TEMPLATE_SPEC_MAX_BYTES = 512 * 1024

_SAFE_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class TemplateUnavailable(Exception):
    """A store template cannot be hired from right now; ``code`` says why.

    ``code`` is a stable machine-readable word the hire route answers with:
    ``app_not_installed``, ``app_disabled``, ``app_admission_denied``,
    ``template_not_offered``, ``template_invalid``,
    ``template_spec_unreadable``, ``template_not_materialized``.
    """

    def __init__(self, code: str, message: str, *, status: int = 404):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class StoreTemplate:
    """Everything a hire needs from one store listing, resolved once."""

    app: str
    version: str
    card: CrewTemplate
    #: The declared agent name (spec ``name`` else the file stem).
    agent_name: str
    #: The materialized agent file's stem: ``<app>--<agent_name>``. This is the
    #: ``kiro_agent`` the member is created against and then forked from.
    materialized: str
    #: The template's agent definition as shipped inside the app.
    spec: dict[str, Any] = field(default_factory=dict)
    #: The definition as MATERIALIZED -- the shipped spec after the app bridge
    #: added the app's own MCP servers, the host's managed refs and its policy --
    #: read ONCE at resolve time (and, for a pinned card, verified against the
    #: bridge's rendering). This is the dict the hire copies into the member's
    #: own file (no path is reopened after the checks), so it is the pristine
    #: BASE a role update merges against and the THEIRS it merges in; comparing
    #: the member against the raw shipped spec would read the bridge's plumbing
    #: as the member's own customization.
    materialized_spec: dict[str, Any] = field(default_factory=dict)
    initial_briefing: str = ""

    @property
    def ref(self) -> str:
        return template_ref(self.app, self.agent_name)


def template_ref(app: str, agent_name: str) -> str:
    """The ``template`` a wrapper row records: ``<app>/<agent>``."""
    return _namespace(app, agent_name)


def _read_bytes_pinned(path: Path, cap: int, *, what: str) -> bytes | None:
    """The exact bytes of an app file (for a content digest), or ``None`` for a
    missing, refused or oversized one -- the same pinned read as the text form,
    without the lossy decode."""
    try:
        data = read_bytes_pinned(path, what=what, max_bytes=cap + 1)
    except (OSError, ValueError):
        return None
    return None if len(data) > cap else data


def content_digest(data: bytes) -> str:
    """The form a card's ``digests`` pins: ``sha256:<hex>`` over the file's bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _verify_card_content(
    app: str, card: CrewTemplate, root: Path, *, signature_required: bool
) -> dict[str, bytes]:
    """Refuse a card whose agent file or briefing is not the bytes it pins.

    The manifest signature covers the card, not the files the card points at;
    the card's ``digests`` are what carry the publisher's signature to the agent
    definition and the briefing -- text that becomes the member's own definition
    and prompt. So: a declared digest that does not match the file on disk NOW
    refuses the hire (``template_tampered``); a fleet whose admission policy
    requires a signature also refuses a card that pins nothing
    (``template_unverified``) -- for that fleet, a signature that does not reach
    the instructions is not the verification it asks for. A fleet without that
    requirement hires an undigested card as before. Returns the VERIFIED bytes by
    key (``agent``, ``initial_briefing``): the caller parses and copies those,
    never a re-read of the path -- an app that swaps the file after the check
    would otherwise have the swapped bytes hired.
    """
    verified: dict[str, bytes] = {}
    if signature_required:
        # A pinned card is pinned WHOLE: the agent file, and the briefing when
        # the card names one. A card pinning only its briefing (or only its
        # agent) would leave the other file's bytes unauthenticated under a
        # signature that reads as verifying them.
        missing = [
            key
            for key, rel in (("agent", card.agent), ("initial_briefing", card.initial_briefing))
            if rel and not card.digests.get(key)
        ]
        if missing:
            raise TemplateUnavailable(
                "template_unverified",
                f"App '{app}' signs its manifest but pins no content digest for "
                f"{' / '.join(missing)} of {card.agent!r}; this fleet requires every file "
                "the card points at to be pinned",
                status=409,
            )
    if not card.digests:
        return verified
    for key, rel in (("agent", card.agent), ("initial_briefing", card.initial_briefing)):
        want = card.digests.get(key)
        if not want or not rel:
            continue
        data = _read_bytes_pinned(
            root / rel,
            TEMPLATE_SPEC_MAX_BYTES if key == "agent" else INITIAL_BRIEFING_MAX_BYTES,
            what=f"template {key.replace('_', ' ')}",
        )
        if data is None or content_digest(data) != want:
            raise TemplateUnavailable(
                "template_tampered",
                f"App '{app}': {rel!r} is not the file the card's digest pins; "
                "reinstall the app",
                status=409,
            )
        verified[key] = data
    return verified


def _parse_spec_bytes(data: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _verify_materialized_matches(
    app: str,
    root: Path,
    agent_path: str,
    on_disk: dict[str, Any],
    agent_name: str,
    *,
    shipped_text: str | None,
) -> None:
    """Refuse a hire whose materialized file (*on_disk*: read once by the caller,
    the very dict the hire copies) is not the bridge's rendering of the verified
    shipped definition. The rendering starts from *shipped_text* -- the bytes the
    digest check verified -- when the card pinned them, never from a re-read of
    the shipped path an app could swap in between. Compared as parsed JSON, so
    formatting is free and every field -- prompt, tools, hooks, MCP commands --
    is not."""
    rendered = render_app_agent_spec(
        app, root, agent_path, keep_user_edits=False, shipped_text=shipped_text
    )
    if rendered is None or rendered[1] != on_disk:
        raise TemplateUnavailable(
            "template_tampered",
            f"App '{app}': the installed agent {agent_name!r} is not the app's shipped "
            "definition as this host renders it; re-enable the app to restore it",
            status=409,
        )


def _read_text_pinned(path: Path, cap: int, *, what: str) -> str | None:
    """Read an app- or member-controlled file without following a planted link.

    An installed app's tree is the app's to change between install and hire, and
    a member's directory is agent-writable: a by-name ``read_text`` on either is
    a disclosure primitive pointed at whatever the name resolves to by then --
    replace the briefing with a symlink to a credential file and the next hire
    copies that credential into a prompt-visible briefing. ``read_file_pinned``
    pins the ancestors and refuses a non-regular final component. ``None`` for a
    missing, refused, oversized or undecodable file.
    """
    try:
        text = read_file_pinned(path, what=what, max_bytes=cap + 1)
    except (OSError, ValueError):
        return None
    if len(text.encode("utf-8", "surrogatepass")) > cap:
        return None
    return text


def _read_json_capped(path: Path, cap: int, *, what: str) -> dict[str, Any] | None:
    text = _read_text_pinned(path, cap, what=what)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def resolve_store_template(app: str, agent_path: str) -> StoreTemplate:
    """Resolve ``{app, agent}`` to a hireable :class:`StoreTemplate`.

    ``agent_path`` is the manifest ``agents`` entry the card names. Raises
    :class:`TemplateUnavailable` for every way the listing cannot be hired
    from: the app is not installed or not enabled (only an enabled app has its
    agents materialized), the manifest offers no card for that agent, the
    shipped spec is unreadable, or the materialized file is missing.
    """
    if not isinstance(app, str) or not _SAFE_APP_NAME_RE.match(app):
        raise TemplateUnavailable("app_not_installed", "source.app must name an installed app")
    manifest = get_app_manifest(app)
    if manifest is None:
        raise TemplateUnavailable("app_not_installed", f"App '{app}' is not installed")
    meta = _read_installed(app)
    if meta is None or not meta.enabled:
        raise TemplateUnavailable(
            "app_disabled", f"App '{app}' is disabled; enable it to hire from it", status=409
        )
    # Admission again, against the manifest on disk NOW. Install, update and
    # enable each run it, but the card's role, triggers and briefing become
    # prompt-adjacent text on the member, and the manifest they come from is the
    # app's to rewrite after enable -- a signed manifest edited afterwards
    # carries a signature that fails to verify, and a fleet that requires one
    # must not see its text hired in. Builtins are exempt exactly as enable
    # exempts them.
    signature_required = False
    if meta.origin != "builtin":
        denied = app_admission_denied(app, manifest=manifest, action="hire")
        if denied:
            raise TemplateUnavailable(
                "app_admission_denied",
                f"App '{app}' is blocked by the admission policy: {denied}",
                status=409,
            )
        # Through the module, like ``app_admission_denied`` reads it: one policy
        # source for the deny and for what the deny's absence implies.
        signature_required = _admission.load_app_admission_policy().require_signature
    card = next((t for t in manifest.crew.templates if t.agent == agent_path), None)
    if card is None:
        raise TemplateUnavailable(
            "template_not_offered", f"App '{app}' offers no template for {agent_path!r}"
        )
    # The manifest on disk NOW, not the one install validated: an app's tree is
    # the app's to change afterwards, and a card path rewritten to ``../../.env``
    # would otherwise be read (pinned reads contain links, not traversal). The
    # same checks install runs, against the same root.
    root = app_dir(app)
    problems = manifest.crew.validate(manifest.agents, root)
    if problems or any(
        _path_escapes_app_root(p, root) for p in (agent_path, card.initial_briefing) if p
    ):
        raise TemplateUnavailable(
            "template_invalid",
            f"App '{app}' ships a crew section that fails validation; reinstall it",
            status=409,
        )
    # The signature reaches the FILES only through the card's digests: verified
    # against the bytes on disk now, before anything is read as a spec.
    verified = _verify_card_content(app, card, root, signature_required=signature_required)
    spec_path = root / agent_path
    # Parsed from the VERIFIED bytes when the card pinned them -- never a
    # second read of the path, which an app could have swapped in between.
    spec = (
        _parse_spec_bytes(verified["agent"])
        if "agent" in verified
        else _read_json_capped(spec_path, TEMPLATE_SPEC_MAX_BYTES, what="template agent file")
    )
    if spec is None:
        raise TemplateUnavailable(
            "template_spec_unreadable",
            f"The template's agent file {agent_path!r} could not be read",
            status=409,
        )
    declared = spec.get("name")
    agent_name = declared if isinstance(declared, str) and declared else Path(agent_path).stem
    materialized = _safe_link_name(_namespace(app, agent_name))
    materialized_path = kiro_agents_dir_path() / f"{materialized}.json"
    if not materialized_path.is_file():
        raise TemplateUnavailable(
            "template_not_materialized",
            f"App '{app}' has not installed its agent {agent_name!r} yet; re-enable the app",
            status=409,
        )
    # Read ONCE: this dict is what the hire copies into the member's own file
    # (the create takes it as ``copy_spec``), so no path is reopened after the
    # checks below and the bytes checked are the bytes copied.
    materialized_spec = _read_json_capped(
        materialized_path, TEMPLATE_SPEC_MAX_BYTES, what="materialized template agent file"
    )
    if materialized_spec is None:
        raise TemplateUnavailable(
            "template_not_materialized",
            f"App '{app}': the installed agent file {materialized!r} could not be read; "
            "re-enable the app",
            status=409,
        )
    if card.digests or signature_required:
        # The digests authenticate the SHIPPED file; the hire COPIES the
        # materialized one, which lives in the agent-writable agents directory.
        # So the materialized file must be exactly what the bridge renders from
        # the verified shipped definition NOW (own servers, managed refs, the
        # per-app MCP policy and prompt -- host state, none of it the file's to
        # carry) -- a hand edit to a pinned app's shared template, or an agent's
        # rewrite of it, is refused rather than copied as the publisher's.
        _verify_materialized_matches(
            app,
            root,
            agent_path,
            materialized_spec,
            agent_name,
            shipped_text=verified["agent"].decode("utf-8") if "agent" in verified else None,
        )
    briefing = ""
    if card.initial_briefing:
        if "initial_briefing" in verified:
            # The verified bytes, decoded the way the pinned read decodes.
            text: str | None = verified["initial_briefing"].decode("utf-8", errors="replace")
        else:
            text = _read_text_pinned(
                root / card.initial_briefing,
                INITIAL_BRIEFING_MAX_BYTES,
                what="template initial briefing",
            )
        if text is None:
            logger.warning(
                "template %s/%s: initial_briefing missing, oversized or not a regular file; "
                "not seeded",
                app,
                agent_name,
            )
        else:
            briefing = text
    return StoreTemplate(
        app=app,
        version=manifest.version,
        card=card,
        agent_name=agent_name,
        materialized=materialized,
        spec=spec,
        materialized_spec=materialized_spec,
        initial_briefing=briefing,
    )


def resolve_template_ref(ref: str) -> StoreTemplate:
    """Resolve a wrapper row's ``template`` (``<app>/<agent name>``) to the listing.

    The row records the DECLARED agent name, not the manifest path the card
    names, so the card is found by resolving each card of the app and matching
    the name it declares; the path's stem is tried first because it is the
    common case and costs no extra read. Raises :class:`TemplateUnavailable`
    with the same codes as :func:`resolve_store_template`, plus
    ``template_not_offered`` when no card of the app declares that name.
    """
    app, sep, agent_name = ref.partition("/")
    if not sep or not agent_name or not isinstance(app, str) or not _SAFE_APP_NAME_RE.match(app):
        raise TemplateUnavailable("template_not_offered", f"{ref!r} does not name a template")
    manifest = get_app_manifest(app)
    if manifest is None:
        raise TemplateUnavailable("app_not_installed", f"App '{app}' is not installed")
    cards = list(manifest.crew.templates)
    cards.sort(key=lambda c: Path(c.agent).stem != agent_name)
    matches: list[StoreTemplate] = []
    # The refusal of the card that NAMES this agent, kept aside: a card whose
    # agent file's stem is the name we resolve is the one the ref points at,
    # and its own verdict (not materialized, unreadable spec, tampered) is the
    # answer when no other card turns out to register under that name --
    # swallowing it would report `template_not_offered` for a template the app
    # does offer, and hide a tampering refusal behind a wrong-name message.
    named_refusal: TemplateUnavailable | None = None
    for card in cards:
        try:
            template = resolve_store_template(app, card.agent)
        except TemplateUnavailable as exc:
            # The app's own verdicts are the answer, whichever card raised
            # them: disabled, banned, or a crew section that fails to
            # validate. Another card's own trouble (unreadable spec, not
            # materialized) is skipped in the search for the named agent.
            if exc.code in ("app_disabled", "app_admission_denied", "template_invalid"):
                raise
            if named_refusal is None and Path(card.agent).stem == agent_name:
                named_refusal = exc
            continue
        if template.agent_name == agent_name:
            matches.append(template)
    if len(matches) > 1:
        # Two cards whose agents register under one name: the manifest
        # validator refuses this at install and at every re-validation, but a
        # ref is resolved from a stored string, so the answer is checked here
        # too rather than picking a card by list order.
        raise TemplateUnavailable(
            "template_ambiguous",
            f"App '{app}' offers more than one template named {agent_name!r}; "
            "the app must give its agents distinct names",
            status=409,
        )
    if matches:
        return matches[0]
    if named_refusal is not None:
        raise named_refusal
    raise TemplateUnavailable(
        "template_not_offered", f"App '{app}' offers no template named {agent_name!r}"
    )


def pristine_copies_root() -> Path:
    return data_home() / PRISTINE_COPIES_DIR_NAME


def pristine_copy_path(member_id: str) -> Path:
    """Absolute path of one member's pristine copy, containment-checked.

    Keyed by the immutable member ID (the config key, inside the agent-name
    grammar, so the filename cannot traverse), never by the lossy slug. Lives
    in the OS-hidden top-level leaf ``PRISTINE_COPIES_DIR_NAME`` describes:
    neither a sandboxed process nor an agent file tool can reach it, the
    gateway opens it directly.
    """
    if not isinstance(member_id, str) or not _AGENT_NAME_RE.match(member_id):
        raise members.MemberSlugError(f"invalid member id {member_id!r}")
    root = pristine_copies_root().resolve()
    target = (root / f"{member_id}.json").resolve()
    if target.parent != root:
        raise members.MemberSlugError(f"member id {member_id!r} escapes {root}")
    return target


def read_pristine_copy(member_id: str, *, generation: str) -> dict[str, Any] | None:
    """The pristine copy as :func:`write_pristine_copy` left it, or ``None``.

    ``None`` when the member has none (hired before templates existed, or from
    a local file), when it cannot be read through the pinned path, when it does
    not have the shape the writer produces, or when it was written for another
    member: the file must name THIS member id and THIS private-store
    *generation* (the store name the create minted, unique per creation), so a
    same-id member deleted and re-hired, or a same-name file on a
    case-insensitive filesystem, never lends its BASE to the wrong member.
    """
    try:
        data = _read_json_capped(
            pristine_copy_path(member_id), TEMPLATE_SPEC_MAX_BYTES, what="member pristine copy"
        )
    except members.MemberSlugError:
        return None
    if data is None:
        return None
    if data.get("member") != member_id or data.get("generation") != generation:
        return None
    if not isinstance(data.get("template"), str) or not isinstance(data.get("version"), str):
        return None
    if not isinstance(data.get("agent"), dict) or not isinstance(data.get("card"), dict):
        return None
    return data


def remove_pristine_copy(member_id: str) -> None:
    """Remove a member's pristine copy; a missing one is nothing to remove."""
    try:
        pristine_copy_path(member_id).unlink(missing_ok=True)
    except members.MemberSlugError:
        return


#: A field one side does not have at all. Distinct from ``None`` (a key set to
#: JSON null is a value) so "removed the key" and "set it to null" merge apart.
MISSING: Any = object()

#: The card fields a role update merges alongside the agent definition.
CARD_FIELDS = ("role", "triggers")

#: Field-state vocabulary the plan reports and the frontend renders.
UNCHANGED = "unchanged"
APPLY = "apply"  # only THEIRS changed: the update applies it
KEEP = "keep"  # only MINE changed: the member's customization stays
AGREE = "agree"  # both changed to the same value: nothing to decide
CONFLICT = "conflict"  # both changed apart: the user picks


@dataclass
class FieldDelta:
    """One mergeable field across the three sides."""

    #: ``spec.<key>`` for an agent-file key, ``card.role`` / ``card.triggers``.
    field: str
    state: str
    base: Any = MISSING
    mine: Any = MISSING
    theirs: Any = MISSING

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"field": self.field, "state": self.state}
        for side in ("base", "mine", "theirs"):
            value = getattr(self, side)
            if value is not MISSING:
                out[side] = value
        return out


class UnresolvedConflicts(Exception):
    """A merge was asked to apply while conflicting fields had no resolution."""

    def __init__(self, fields: list[str]):
        super().__init__(f"unresolved conflicts: {', '.join(fields)}")
        self.fields = fields


def _classify(base: Any, mine: Any, theirs: Any) -> str:
    mine_changed = mine != base
    theirs_changed = theirs != base
    if not mine_changed and not theirs_changed:
        return UNCHANGED
    if theirs_changed and not mine_changed:
        return APPLY
    if mine_changed and not theirs_changed:
        return KEEP
    return AGREE if mine == theirs else CONFLICT


def plan_role_update(
    pristine: dict[str, Any],
    mine_spec: dict[str, Any],
    mine_card: dict[str, Any],
    theirs: StoreTemplate,
) -> list[FieldDelta]:
    """Compare BASE (the pristine copy), MINE (the member) and THEIRS (the
    template as installed now) field by field.

    Fields are the union of the agent-definition keys on the three sides --
    THEIRS being the template as MATERIALIZED, the same form the hire copied and
    the pristine copy recorded, so the bridge's plumbing (the app's own MCP
    servers, the host's managed refs) reads as unchanged rather than as the
    member's edit -- except ``name`` -- the member's file declares its own id,
    the template's declares the template's, and neither is anybody's
    customization -- plus
    the card's ``role`` and ``triggers``. Equality is JSON-value equality on
    the whole field: a list of tools that gained one entry is one changed
    field, not a per-entry merge, because a definition is what the author
    reviewed as a whole. Order is stable (spec keys sorted, card last) so the
    plan the client saw is the plan the apply re-derives.
    """
    raw_spec, raw_card = pristine.get("agent"), pristine.get("card")
    base_spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    base_card: dict[str, Any] = raw_card if isinstance(raw_card, dict) else {}
    deltas: list[FieldDelta] = []
    theirs_spec = theirs.materialized_spec
    keys = set(base_spec) | set(mine_spec) | set(theirs_spec)
    keys.discard("name")
    for key in sorted(keys):
        b, m, t = (
            base_spec.get(key, MISSING),
            mine_spec.get(key, MISSING),
            theirs_spec.get(key, MISSING),
        )
        deltas.append(FieldDelta(f"spec.{key}", _classify(b, m, t), b, m, t))
    theirs_card = {"role": theirs.card.role, "triggers": theirs.card.triggers}
    for key in CARD_FIELDS:
        b = base_card.get(key, "")
        m = mine_card.get(key, "")
        t = theirs_card.get(key, "")
        deltas.append(FieldDelta(f"card.{key}", _classify(b, m, t), b, m, t))
    return deltas


def needs_update(deltas: list[FieldDelta]) -> bool:
    """True when applying the plan would change anything on the member."""
    return any(d.state in (APPLY, CONFLICT) for d in deltas)


def merge_role_update(
    mine_spec: dict[str, Any],
    mine_card: dict[str, Any],
    deltas: list[FieldDelta],
    resolutions: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Produce the member's new agent definition and card fields.

    ``resolutions`` maps a conflicting field to ``"mine"`` or ``"theirs"``; a
    conflict without one raises :class:`UnresolvedConflicts` before anything is
    decided, so a partial resolution never half-applies. A resolution for a
    field that is not in conflict is ignored: the plan, not the client, says
    what is in conflict. The member's ``name`` is never touched, and a field
    the template REMOVED (THEIRS missing) is removed from the member when it
    applies.
    """
    unresolved = [
        d.field
        for d in deltas
        if d.state == CONFLICT and resolutions.get(d.field) not in ("mine", "theirs")
    ]
    if unresolved:
        raise UnresolvedConflicts(unresolved)
    spec = dict(mine_spec)
    card = {key: str(mine_card.get(key, "") or "") for key in CARD_FIELDS}
    for d in deltas:
        take_theirs = d.state == APPLY or (
            d.state == CONFLICT and resolutions.get(d.field) == "theirs"
        )
        if not take_theirs:
            continue
        kind, _, key = d.field.partition(".")
        if kind == "spec":
            if d.theirs is MISSING:
                spec.pop(key, None)
            else:
                spec[key] = d.theirs
        elif kind == "card":
            card[key] = str(d.theirs or "")
    return spec, card


def _ensure_members_root() -> None:
    members.members_root().mkdir(parents=True, exist_ok=True)


def write_pristine_copy(member_id: str, template: StoreTemplate, *, generation: str) -> Path:
    """Record the unmodified template payload at the installed version.

    The BASE of a later three-way merge: the agent definition as materialized
    -- the file the hire copied, bridge plumbing included -- plus the card's
    mergeable fields (role, triggers), stamped with the member id and the
    private-store *generation* :func:`read_pristine_copy` checks. Published
    through the pinned writer (the root itself is created by name -- it is the
    gateway's, not agent-named, and normally already materialized owner-only
    before the first sandbox spawn -- and a planted link at the final name is
    refused rather than written through).
    """
    path = pristine_copy_path(member_id)
    payload = {
        "member": member_id,
        "generation": generation,
        "template": template.ref,
        "version": template.version,
        "agent": template.materialized_spec,
        "card": {"role": template.card.role, "triggers": template.card.triggers},
    }
    root = pristine_copies_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(root)
    except OSError:
        logger.debug("could not tighten mode on %s", root, exc_info=True)
    write_file_pinned(
        path, json.dumps(payload, indent=2, ensure_ascii=False), what="member pristine copy"
    )
    return path


def _replace_preexisting_briefing(slug: str, name: str, dir_fd: int) -> None:
    """Remove whatever sits at the briefing name of a member that did not exist.

    The seed runs inside the hire's locked publication, for a member whose row
    landed in this same hold and whose slug no row held before (the slug
    admission refuses a collision), so nothing at the name can be the member's
    lived state: the member directory is agent-writable and the slug is a
    deterministic derivation of the display name, which makes a file planted
    there ahead of the hire an attempt to hand the new member an assignment of
    the planter's choosing. It is removed BY ITS LITERAL NAME (``lstat`` +
    ``unlink`` relative to the pinned directory: a planted link is unlinked
    as a link and its target never touched), recorded, and the template's
    briefing is created in its place. Anything but a regular file or a link
    is refused -- the hire fails closed rather than guess.
    """
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        kind = "link"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        raise OSError(errno.EEXIST, f"unexpected entry at the briefing name of {slug!r}")
    logger.warning(
        "member %s: a %s already sat at the briefing name of a member that did not "
        "exist; replaced with the template's briefing",
        slug,
        kind,
    )
    # The audit trail is the durable trace of the plant; never let it change
    # the outcome (the replacement is the safe direction either way).
    try:
        sel().log_api_access(
            caller="system",
            operation="member_briefing_preexisting_replaced",
            outcome="replaced",
            source="member_hire",
            resources=f"member {slug}: planted {kind} at the briefing name",
        )
    except Exception:  # noqa: BLE001 - audit failure must not decide the hire
        logger.debug("could not record the replaced briefing for %s", slug, exc_info=True)
    os.unlink(name, dir_fd=dir_fd)


def seed_briefing(slug: str, text: str) -> bool:
    """Write a template's initial briefing as the new member's own ``briefing.md``.

    True when the file was written. Called for a member that did not exist
    until the hire's locked publication (see ``_link_member_to_template``), so
    the template's briefing is what the member starts with, whatever sat at
    the name before: a pre-existing regular file or link there is a plant (or
    a leftover of a member that is gone), never this member's lived state, and
    is replaced -- by its literal name, recorded in the security event log
    (:func:`_replace_preexisting_briefing`). The write itself is the CREATE
    (``O_CREAT | O_EXCL | O_NOFOLLOW`` through the pinned member directory):
    it never follows a link and never truncates a target, and a name that is
    taken again after the replacement is refused rather than raced. A platform
    where briefings are not read (no ``O_NOFOLLOW``) gets none, so the member
    is not handed a file the runtime will never inject.
    """
    if not text or not members.member_briefing_supported():
        return False
    path = members.member_briefing_path(slug)
    _ensure_members_root()
    dir_fd = create_and_open_dir_pinned(path.parent, what="member directory")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            _replace_preexisting_briefing(slug, path.name, dir_fd)
            # Once: a name taken again between the unlink and this create is
            # something racing the hire at the member's directory; fail closed.
            fd = os.open(path.name, flags, 0o600, dir_fd=dir_fd)
        try:
            payload = text.encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            # A short write or ENOSPC must not leave a truncated briefing that
            # ``O_EXCL`` then reports as the member's own on every retry.
            os.close(fd)
            fd = -1
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except OSError:
                logger.warning("could not remove a partially seeded briefing for %s", slug)
            raise
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(dir_fd)
    return True
