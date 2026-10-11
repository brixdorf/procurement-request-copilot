"""Data sources behind the tools, with injectable overrides for evaluation.

Production path: CSV/JSON files plus the vendor-risk HTTP service.
Evaluation path: the same interface with synthetic records or a stubbed
vendor-risk fetcher, so edge cases can be tested without editing data files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from src import data_access
from src.vendor_client import VendorRiskResult, ensure_local_vendor_api, fetch_vendor_risk

VendorRiskFetcher = Callable[[str], VendorRiskResult]


def norm(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _records(df: pd.DataFrame) -> list[dict]:
    clean = df.astype(object).where(df.notna(), None)
    return clean.to_dict("records")


@dataclass
class SourceData:
    employees: dict[str, dict]
    budgets: dict[str, dict]
    catalog: list[dict]
    vendors: dict[str, dict]
    purchases: list[dict]
    requests: dict[str, dict]
    vendor_risk_fetcher: VendorRiskFetcher = field(default=fetch_vendor_risk)
    uses_live_service: bool = True

    @classmethod
    def load(cls) -> "SourceData":
        return cls(
            employees={r["employee_id"]: r for r in _records(data_access.load_employees())},
            budgets={norm(r["department"]): r for r in _records(data_access.load_budgets())},
            catalog=_records(data_access.load_software_catalog()),
            vendors={norm(r["vendor_name"]): r for r in _records(data_access.load_vendors())},
            purchases=_records(data_access.load_purchase_history()),
            requests={r["request_id"]: r for r in data_access.load_requests()},
        )

    # -- lookups -----------------------------------------------------------
    def get_request(self, request_id: str) -> dict:
        try:
            return self.requests[request_id]
        except KeyError:
            raise KeyError(f"Unknown request_id: {request_id}") from None

    def employee(self, employee_id: str | None) -> dict | None:
        return self.employees.get(employee_id) if employee_id else None

    def budget(self, department: str | None) -> dict | None:
        return self.budgets.get(norm(department)) if department else None

    def vendor(self, vendor_name: str | None) -> dict | None:
        return self.vendors.get(norm(vendor_name)) if vendor_name else None

    def vendor_risk(self, vendor_name: str) -> VendorRiskResult:
        if self.uses_live_service:
            ensure_local_vendor_api()
        return self.vendor_risk_fetcher(vendor_name)

    # -- evaluation helpers -----------------------------------------------
    def with_overrides(
        self,
        *,
        employees: list[dict] | None = None,
        budgets: list[dict] | None = None,
        catalog: list[dict] | None = None,
        vendors: list[dict] | None = None,
        vendor_risk: dict[str, VendorRiskResult | dict] | None = None,
    ) -> "SourceData":
        """Return a copy with synthetic records added or replaced.

        vendor_risk maps vendor name to either a ready VendorRiskResult or a raw
        record dict (wrapped as an 'ok' result). Vendors not listed fall back to
        the live fetcher.
        """
        copy = SourceData(
            employees={**self.employees, **{r["employee_id"]: r for r in (employees or [])}},
            budgets={**self.budgets, **{norm(r["department"]): r for r in (budgets or [])}},
            catalog=list(self.catalog) + list(catalog or []),
            vendors={**self.vendors, **{norm(r["vendor_name"]): r for r in (vendors or [])}},
            purchases=list(self.purchases),
            requests=dict(self.requests),
            vendor_risk_fetcher=self.vendor_risk_fetcher,
            uses_live_service=self.uses_live_service,
        )
        if vendor_risk:
            stubs = {norm(k): v for k, v in vendor_risk.items()}
            base = self.vendor_risk_fetcher

            def fetcher(name: str) -> VendorRiskResult:
                hit = stubs.get(norm(name))
                if hit is None:
                    return base(name)
                if isinstance(hit, VendorRiskResult):
                    return hit
                return VendorRiskResult("ok", {"vendor_name": name, **hit}, 200, "", 1)

            copy.vendor_risk_fetcher = fetcher
        return copy
