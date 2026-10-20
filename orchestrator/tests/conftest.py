"""Test bootstrap.

The orchestrator imports the Azure SDKs at module scope (as a real Function App
does). Tests that exercise ordering/authorisation logic should not need a live
Azure SDK install, so we stub only the modules that are genuinely optional for
those tests. `azure-cosmos` and `aiohttp` are installed by requirements.txt and
are used for real by the state-machine tests.
"""

import importlib.util
import sys
import types


def _stub_if_missing(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    try:
        if importlib.util.find_spec(name) is not None:
            return
    except ModuleNotFoundError:
        pass  # parent package is itself a stub
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.util.spec_from_loader(name, loader=None)
    mod.__path__ = []          # make it a package so `a.b` imports resolve
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_stub_if_missing("azure.functions", HttpRequest=object, HttpResponse=object,
                 TimerRequest=object)
_stub_if_missing("azure.identity")
_stub_if_missing("azure.identity.aio", DefaultAzureCredential=object)
_stub_if_missing("azure.mgmt")
_stub_if_missing("azure.mgmt.web")
_stub_if_missing("azure.mgmt.web.aio", WebSiteManagementClient=object)
_stub_if_missing("azure.mgmt.sql")
_stub_if_missing("azure.mgmt.sql.aio", SqlManagementClient=object)
_stub_if_missing("azure.mgmt.cdn")
_stub_if_missing("azure.mgmt.cdn.aio", CdnManagementClient=object)
