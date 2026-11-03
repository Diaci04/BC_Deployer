import pytest

from app.bc.base import InstalledApp
from app.bc.manifest import AppManifest, Dependency
from app.bc.ordering import OrderingError, order_by_dependencies
from app.bc.precheck import Action, precheck
from app.bc.version import Version

from .apps import APP_A, APP_B, APP_C, BASE_APP

V = Version.parse
EXT = "eeeeeeee-0000-0000-0000-00000000000e"


def app(app_id, name, version="1.0.0.0", deps=(), application=None):
    return AppManifest(
        id=app_id, name=name, publisher="Contoso", version=V(version),
        dependencies=[Dependency(d[0], d[1], "Contoso", V(d[2] if len(d) > 2 else "1.0.0.0")) for d in deps],
        application=V(application) if application else None,
    )


def names(apps):
    return [a.name for a in apps]


def test_dependencies_come_first():
    a = app(APP_A, "A", deps=[(APP_B, "B")])
    b = app(APP_B, "B", deps=[(APP_C, "C")])
    c = app(APP_C, "C")
    assert names(order_by_dependencies([a, b, c])) == ["C", "B", "A"]


def test_diamond_and_external_dependencies():
    a = app(APP_A, "A", deps=[(APP_B, "B"), (APP_C, "C"), (EXT, "Esterna")])
    b = app(APP_B, "B", deps=[(APP_C, "C")])
    c = app(APP_C, "C")
    assert names(order_by_dependencies([a, c, b])) == ["C", "B", "A"]


def test_independent_apps_are_sorted_by_name():
    assert names(order_by_dependencies([app(APP_B, "zeta"), app(APP_A, "Alfa")])) == ["Alfa", "zeta"]


def test_cycle_is_reported():
    a = app(APP_A, "A", deps=[(APP_B, "B")])
    b = app(APP_B, "B", deps=[(APP_A, "A")])
    with pytest.raises(OrderingError, match="circolare: A → B → A"):
        order_by_dependencies([a, b])


def test_duplicate_app_id_is_rejected():
    with pytest.raises(OrderingError, match="stesso app id"):
        order_by_dependencies([app(APP_A, "A", "1.0.0.0"), app(APP_A, "A", "2.0.0.0")])


# ---------------------------------------------------------------- precheck
def installed(app_id, name, version, is_installed=True):
    return InstalledApp(app_id, name, "Contoso", V(version), is_installed)


def test_precheck_actions():
    apps = [app(APP_A, "A", "2.0.0.0"), app(APP_B, "B", "1.0.0.0"), app(APP_C, "C", "1.0.0.0")]
    env = [installed(APP_A, "A", "1.5.0.0"), installed(APP_B, "B", "1.0.0.0")]
    res = precheck(apps, env)
    assert [p.action for p in res.plans] == [Action.UPGRADE, Action.SAME_VERSION, Action.INSTALL]
    assert res.plans[0].installed_version == V("1.5")
    assert res.ok


def test_precheck_blocks_downgrade():
    res = precheck([app(APP_A, "A", "1.0.0.0")], [installed(APP_A, "A", "1.2.0.0")])
    assert res.plans[0].action == Action.DOWNGRADE
    assert not res.ok and "inferiore" in res.problems[0]


def test_precheck_uninstalled_published_app_counts_as_new():
    res = precheck([app(APP_A, "A", "1.0.0.0")], [installed(APP_A, "A", "2.0.0.0", is_installed=False)])
    assert res.plans[0].action == Action.INSTALL and res.ok


def test_precheck_missing_external_dependency():
    res = precheck([app(APP_A, "A", deps=[(EXT, "Esterna", "3.0.0.0")])], [])
    assert not res.ok and "Esterna" in res.problems[0] and "non installata" in res.problems[0]


def test_precheck_external_dependency_too_old():
    res = precheck([app(APP_A, "A", deps=[(EXT, "Esterna", "3.0.0.0")])], [installed(EXT, "Esterna", "2.9.0.0")])
    assert not res.ok and "≥ 3.0.0.0" in res.problems[0] and "2.9.0.0" in res.problems[0]


def test_precheck_dependency_in_set_too_old():
    res = precheck(
        [app(APP_B, "B", "1.0.0.0"), app(APP_A, "A", deps=[(APP_B, "B", "1.1.0.0")])], []
    )
    assert not res.ok and "nel set c'è 1.0.0.0" in res.problems[0]


def test_precheck_base_application_version():
    env = [installed(BASE_APP, "Base Application", "23.5.0.0")]
    res = precheck([app(APP_A, "A", application="24.0.0.0")], env)
    assert not res.ok and "Base Application ≥ 24.0.0.0" in res.problems[0]
    assert precheck([app(APP_A, "A", application="23.0.0.0")], env).ok


def test_precheck_skipped_app_does_not_check_dependencies():
    res = precheck(
        [app(APP_A, "A", "1.0.0.0", deps=[(EXT, "Esterna")])], [installed(APP_A, "A", "1.0.0.0")]
    )
    assert res.ok
