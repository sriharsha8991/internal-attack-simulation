"""Tests for ``PayloadCatalog`` — the planner/master prompt-context shim over
``GET /payloads``.

Covers:
  * filtering by phase + foothold platform
  * known_ids() for push-time validation
  * planner block contains UUIDs verbatim and is empty when no rows match
  * master summary groups by phase and is empty for an empty catalog
  * fetch() swallows transport errors and returns an empty catalog
"""

from __future__ import annotations

from uuid import UUID

import pytest

from bas.agents.payload_catalog import PayloadCatalog
from bas.models import PayloadMetadata


SHARP_ID = UUID("11111111-1111-1111-1111-111111111111")
LDAP_ID = UUID("22222222-2222-2222-2222-222222222222")
LINPEAS_ID = UUID("33333333-3333-3333-3333-333333333333")


def _make_entries() -> list[PayloadMetadata]:
    return [
        PayloadMetadata(
            payload_id=SHARP_ID,
            name="SharpHound.exe",
            platform="windows",
            type="binary",
            risk_classification="high",
            description="AD recon collector",
            category="discovery",
        ),
        PayloadMetadata(
            payload_id=LDAP_ID,
            name="ldapdomaindump.pyz",
            platform="any",
            type="script",
            risk_classification="low",
            description="LDAP dumper",
            category="discovery",
        ),
        PayloadMetadata(
            payload_id=LINPEAS_ID,
            name="linpeas.sh",
            platform="linux",
            type="script",
            risk_classification="medium",
            description="Linux privesc scanner",
            category="privesc",
        ),
    ]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_for_phase_filters_by_category_and_platform():
    cat = PayloadCatalog(entries=_make_entries())
    windows_discovery = cat.for_phase("discovery", "windows")
    names = {e.name for e in windows_discovery}
    # SharpHound matches windows; ldapdomaindump matches "any" (also included)
    assert names == {"SharpHound.exe", "ldapdomaindump.pyz"}


def test_for_phase_excludes_wrong_platform():
    cat = PayloadCatalog(entries=_make_entries())
    linux_discovery = cat.for_phase("discovery", "linux")
    names = {e.name for e in linux_discovery}
    # Windows-only SharpHound dropped; "any"-platform ldapdomaindump kept.
    assert names == {"ldapdomaindump.pyz"}


def test_for_phase_unknown_platform_keeps_all_category_matches():
    cat = PayloadCatalog(entries=_make_entries())
    # When platform is unknown we cannot filter — return all category matches.
    discovery = cat.for_phase("discovery", None)
    assert {e.name for e in discovery} == {"SharpHound.exe", "ldapdomaindump.pyz"}


def test_for_phase_unknown_phase_returns_empty():
    cat = PayloadCatalog(entries=_make_entries())
    # nonexistent_phase will only return uncategorized payloads (if any).
    # Since all payloads in _make_entries have a category, it should be empty.
    assert cat.for_phase("nonexistent_phase", "windows") == []
    # If phase is None, it acts as a global list and returns all payloads for the platform.
    assert len(cat.for_phase(None, "windows")) == 2
    assert len(cat.for_phase("", "windows")) == 2


def test_for_phase_is_case_insensitive():
    cat = PayloadCatalog(entries=_make_entries())
    assert len(cat.for_phase("DISCOVERY", "WINDOWS")) == 2


# ---------------------------------------------------------------------------
# known_ids
# ---------------------------------------------------------------------------


def test_known_ids_returns_set_of_string_uuids():
    cat = PayloadCatalog(entries=_make_entries())
    ids = cat.known_ids()
    assert str(SHARP_ID) in ids
    assert str(LDAP_ID) in ids
    assert str(LINPEAS_ID) in ids
    assert len(ids) == 3


def test_known_ids_skips_entries_without_id():
    cat = PayloadCatalog(
        entries=[
            PayloadMetadata(
                name="orphan",
                category="discovery",
                description="no id assigned",
            ),
        ]
    )
    assert cat.known_ids() == set()


# ---------------------------------------------------------------------------
# render_planner_block
# ---------------------------------------------------------------------------


def test_render_planner_block_contains_uuid_verbatim():
    cat = PayloadCatalog(entries=_make_entries())
    block = cat.render_planner_block("discovery", "windows")
    assert str(SHARP_ID) in block
    assert str(LDAP_ID) in block
    assert "SharpHound.exe" in block
    # The anti-hallucination rules must be present.
    assert "NEVER invent a payload_id" in block
    # Wrong-phase IDs must not leak into a discovery-only block.
    assert str(LINPEAS_ID) not in block


def test_render_planner_block_empty_when_no_match():
    cat = PayloadCatalog(entries=_make_entries())
    # No payloads at all categorised under "impact".
    assert cat.render_planner_block("impact", "windows") == ""
    # privesc has only linpeas (linux); windows foothold filters it out.
    assert cat.render_planner_block("privesc", "windows") == ""


def test_render_planner_block_empty_catalog():
    assert PayloadCatalog().render_planner_block("discovery", "windows") == ""


# ---------------------------------------------------------------------------
# render_master_summary
# ---------------------------------------------------------------------------


def test_render_master_summary_groups_by_phase():
    cat = PayloadCatalog(entries=_make_entries())
    summary = cat.render_master_summary()
    assert "discovery" in summary
    assert "privesc" in summary
    assert "SharpHound.exe" in summary
    assert "linpeas.sh" in summary


def test_render_master_summary_empty_catalog():
    assert PayloadCatalog().render_master_summary() == ""


def test_render_master_summary_groups_uncategorized_entries():
    cat = PayloadCatalog(
        entries=[
            PayloadMetadata(name="uncategorised", platform="windows"),
        ]
    )
    summary = cat.render_master_summary()
    assert "- uncategorized/all: uncategorised (windows)" in summary


# ---------------------------------------------------------------------------
# fetch() — swallows transport failures
# ---------------------------------------------------------------------------


class _Boom:
    def list(self):
        raise RuntimeError("backend unreachable")


class _OK:
    def list(self):
        return _make_entries()


class _FakeBas:
    def __init__(self, payloads):
        self.payloads = payloads


def test_fetch_returns_empty_on_transport_failure():
    cat = PayloadCatalog.fetch(_FakeBas(_Boom()))
    assert cat.entries == []
    # The downstream prompt rendering should also be empty — no header is
    # better than a misleading "0 payloads" header.
    assert cat.render_planner_block("discovery", "windows") == ""
    assert cat.render_master_summary() == ""


def test_fetch_populates_from_payloads_list():
    cat = PayloadCatalog.fetch(_FakeBas(_OK()))
    assert len(cat.entries) == 3
    assert str(SHARP_ID) in cat.known_ids()


# ---------------------------------------------------------------------------
# push_specialist drops hallucinated payload_ids
# ---------------------------------------------------------------------------


def test_push_specialist_drops_unknown_payload_id(monkeypatch):
    """A planner that emits a syntactically-valid but unknown UUID must have
    that ID nulled before the stage POST — Pydantic only validates UUID format,
    not membership in the backend catalog."""
    from uuid import UUID, uuid4

    from bas.agents.specialist import SpecialistPlan, push_specialist
    from bas.models import (
        AbilityCreate,
        AbilityResponse,
        AbilityStageCreate,
        AbilityStageResponse,
        AdversaryCreate,
        AdversaryResponse,
        GeneratedAbility,
    )

    catalog = PayloadCatalog(entries=_make_entries())
    hallucinated = uuid4()  # syntactically valid, not in the catalog

    plan = SpecialistPlan(
        adversary=AdversaryCreate(name="test-adv"),
        abilities=[
            GeneratedAbility(
                ability=AbilityCreate(name="test-ab", platform="windows"),
                stages=[
                    AbilityStageCreate(
                        stage_name="legit",
                        stage_order=1,
                        executor="cmd",
                        command_template="SharpHound.exe",
                        payload_id=SHARP_ID,  # real, must survive
                    ),
                    AbilityStageCreate(
                        stage_name="bogus",
                        stage_order=2,
                        executor="cmd",
                        command_template="fakebin.exe",
                        payload_id=hallucinated,  # must be nulled
                    ),
                ],
                rationale="test",
                grounding_depth="skip",
                provider="test",
            ),
        ],
    )

    captured_stages: list[AbilityStageCreate] = []

    class _FakeAbilities:
        def create(self, ability):
            return AbilityResponse(
                ability_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                name=ability.name,
            )

        def create_stage(self, ability_id, stage):
            captured_stages.append(stage)
            return AbilityStageResponse(
                stage_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                ability_id=UUID(str(ability_id)),
                stage_name=stage.stage_name,
                stage_order=stage.stage_order,
                payload_id=stage.payload_id,
            )

    class _FakeAdversaries:
        def create(self, adv):
            return AdversaryResponse(
                adversary_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
                name=adv.name,
            )

        def link_ability(self, adv_id, ab_id):
            return True

    class _FakeBasClient:
        abilities = _FakeAbilities()
        adversaries = _FakeAdversaries()

    class _FakeSkillTool:
        def has(self, name):
            return True

    state = {"next_stage": "fake-skill", "run_id": "engagement-test"}

    result = push_specialist(
        state,
        plan=plan,
        skill_tool=_FakeSkillTool(),
        bas=_FakeBasClient(),
        artifacts=None,
        catalog=catalog,
    )

    assert result.success
    assert len(captured_stages) == 2
    # Legit payload survives.
    assert captured_stages[0].payload_id == SHARP_ID
    # Hallucinated payload was nulled out before POST.
    assert captured_stages[1].payload_id is None
