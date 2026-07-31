from __future__ import annotations

from importlib import import_module

from job_agent.browser.manager import BrowserManager
from job_agent.sites.base import BaseSiteAdapter
from job_agent.sites.geekjob.adapter import GeekJobAdapter
from job_agent.sites.habr.adapter import HabrAdapter
from job_agent.sites.hh.adapter import HHAdapter
from job_agent.sites.kwork.adapter import KworkAdapter


def build_adapter(name: str, browser: BrowserManager, profile: str = "hh") -> BaseSiteAdapter:
    adapters = {"hh": HHAdapter, "geekjob": GeekJobAdapter, "habr": HabrAdapter, "kwork": KworkAdapter}
    adapter_type = adapters.get(name)
    if adapter_type is None and ":" in name:
        module_name, class_name = name.split(":", 1)
        try:
            adapter_type = getattr(import_module(module_name), class_name)
        except (ImportError, AttributeError) as exc:
            raise ValueError(f"Could not load custom adapter: {name}") from exc
    if adapter_type is None:
        raise ValueError(f"Unsupported adapter: {name}")
    adapter = adapter_type(browser, profile)
    if not isinstance(adapter, BaseSiteAdapter):
        raise ValueError(f"Custom adapter does not implement BaseSiteAdapter: {name}")
    return adapter
