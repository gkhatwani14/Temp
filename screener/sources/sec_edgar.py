"""SEC EDGAR adapter -- filing-sourced fundamentals and point-in-time history.

Provider: SEC EDGAR public XBRL APIs (hosts data.sec.gov and www.sec.gov).
    https://www.sec.gov/files/company_tickers.json            ticker -> CIK
    https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   every non-dimensional XBRL fact
    https://data.sec.gov/submissions/CIK##########.json       filing list (forms, dates, 8-K items)
    https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/R#.htm
        the financial statements as rendered by EDGAR for one filing ("R pages"). Used for
        (a) filings that the companyfacts API does not (yet) carry, (b) the balance-sheet
        face, to decide which debt line items to sum (and to pick up company-specific tags
        that the API never carries), (c) cash-flow D&A rows when D&A is only tagged with a
        company-specific element.

Everything is parsed from what the filer reported; nothing is estimated.  A value that
cannot be sourced is None.  Conventions that matter downstream:

* Flow items (revenue, operating income, cash flow ...) are reported year-to-date in 10-Qs
  and full-year in 10-Ks.  Discrete periods are derived from fact durations (start/end):
  a directly reported three-month fact when there is one, otherwise YTD(k) - YTD(k-1)
  (so Q2 = 6M - Q1, Q4 = FY - 9M), each derivation staying inside ONE tag.
* When the same period is reported in several filings the LATEST filing wins
  (`prefer="latest"`, i.e. restated values); `prefer="first"` gives as-originally-reported.
* Per-share values and share counts are put on the CURRENT share basis: stock splits are
  detected from restated weighted-average share counts / EPS, and values that come from
  filings made before a split are adjusted (`*_as_reported` keeps the raw number).
* `filed` on a period record is the date the first periodic report (10-Q/10-K/20-F/6-K)
  covering that period end was filed, i.e. when the numbers became public.

CLI:  python3 -m screener.sources.sec_edgar NVDA LOPE [--full] [--no-pages]
"""
from __future__ import annotations

import calendar as _calendar
import html as _html
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import lru_cache

from bs4 import BeautifulSoup

from ..http import SEC_UA, FetchError, fetch, fetch_json

PROVIDER = "SEC EDGAR"
_H = {"User-Agent": SEC_UA}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{name}"

# A registrant that re-organised under a new CIK keeps its older history under the old one.
# (EDGAR has no machine-readable successor link, so this is a verified manual map; the
# predecessor's entity name is checked at load time and reported in `notes`.)
PREDECESSOR_CIKS: dict[str, list[int]] = {
    "CLMT": [1340122],  # Calumet Specialty Products Partners, L.P. -> Calumet, Inc. (2024 C-corp conversion)
}

_PERIODIC = ("10-K", "10-Q", "20-F", "40-F", "6-K")
_ANNUAL = ("10-K", "20-F", "40-F")
_EXCLUDED_FORMS = ("DEF", "PRE", "PX", "ARS")  # proxy material carries pay-vs-performance facts only
_TOL = timedelta(days=10)  # tolerance when matching period START dates (52/53-week calendars, R pages)


# --------------------------------------------------------------------------- concepts
def _t(tag: str, tax: str = "us-gaap") -> tuple:
    return ("T", tax, tag)


def _sum(label: str, *specs) -> tuple:
    return ("SUM", label, specs)


def _neg(label: str, spec) -> tuple:
    return ("NEG", label, spec)


# Ordered alternatives per concept.  For each period the first alternative that yields a value wins.
FLOW_SPECS: dict[str, list[tuple]] = {
    "revenue": [
        _t("Revenues"), _t("RevenueFromContractWithCustomerExcludingAssessedTax"),
        _t("RevenueFromContractWithCustomerIncludingAssessedTax"), _t("SalesRevenueNet"),
        _t("SalesRevenueGoodsNet"), _t("SalesRevenueServicesNet"), _t("RevenuesNetOfInterestExpense"),
        _t("RegulatedAndUnregulatedOperatingRevenue"), _t("RealEstateRevenueNet"),
        _t("OperatingLeasesIncomeStatementLeaseRevenue"), _t("Revenue", "ifrs-full"),
        _sum("net interest income + noninterest income (bank-style revenue)",
             _t("InterestIncomeExpenseNet"), _t("NoninterestIncome")),
    ],
    "cost_of_revenue": [
        _t("CostOfRevenue"), _t("CostOfGoodsAndServicesSold"), _t("CostOfGoodsSold"), _t("CostOfServices"),
        _t("CostOfSales", "ifrs-full"),
    ],
    "gross_profit": [_t("GrossProfit"), _t("GrossProfit", "ifrs-full")],
    "operating_income": [_t("OperatingIncomeLoss"), _t("ProfitLossFromOperatingActivities", "ifrs-full")],
    "pretax_income": [
        _t("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
        _t("IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
        _t("IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"),
        _t("ProfitLossBeforeTax", "ifrs-full"),
    ],
    "depreciation_amortization": [
        _t("DepreciationDepletionAndAmortization"), _t("DepreciationAndAmortization"),
        _t("DepreciationAmortizationAndAccretionNet"),
        _t("UtilitiesOperatingExpenseDepreciationAndAmortization"),
        _t("DepreciationAndAmortisationExpense", "ifrs-full"),
        _t("AdjustmentsForDepreciationAndAmortisationExpense", "ifrs-full"),
        ("CFROWS", "cash-flow statement rows labelled depreciation/amortization (EDGAR R page)"),
        _sum("Depreciation + AdjustmentForAmortization", _t("Depreciation"), _t("AdjustmentForAmortization")),
        _sum("Depreciation + AmortizationOfIntangibleAssets", _t("Depreciation"),
             _t("AmortizationOfIntangibleAssets")),
        _t("Depreciation"),
    ],
    "net_income": [
        _t("NetIncomeLoss"), _t("ProfitLoss"), _t("NetIncomeLossAvailableToCommonStockholdersBasic"),
        _t("ProfitLossAttributableToOwnersOfParent", "ifrs-full"), _t("ProfitLoss", "ifrs-full"),
    ],
    "interest_expense": [
        _t("InterestExpense"), _t("InterestExpenseNonoperating"), _t("InterestExpenseDebt"),
        _t("InterestAndDebtExpense"), _t("InterestExpenseBorrowings"), _t("InterestExpenseLongTermDebt"),
        _t("InterestExpenseOperating"), _t("InterestExpense", "ifrs-full"), _t("FinanceCosts", "ifrs-full"),
        _neg("-(InterestIncomeExpenseNonoperatingNet): NET of interest income", _t("InterestIncomeExpenseNonoperatingNet")),
        _neg("-(InterestIncomeExpenseNet): NET of interest income", _t("InterestIncomeExpenseNet")),
    ],
    "operating_cash_flow": [
        _t("NetCashProvidedByUsedInOperatingActivities"),
        _t("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        _t("CashFlowsFromUsedInOperatingActivities", "ifrs-full"),
    ],
    "capex": [
        _t("PaymentsToAcquirePropertyPlantAndEquipment"), _t("PaymentsToAcquireProductiveAssets"),
        _t("PaymentsToAcquireOtherPropertyPlantAndEquipment"), _t("PaymentsForCapitalImprovements"),
        _t("PaymentsToAcquireRealEstate"),
        _t("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "ifrs-full"),
    ],
    "capitalized_software": [_t("PaymentsToDevelopSoftware"), _t("PaymentsForSoftware")],
}
PER_SHARE_SPECS: dict[str, list[tuple]] = {
    "eps_diluted": [
        _t("EarningsPerShareDiluted"), _t("EarningsPerShareBasicAndDiluted"),
        _t("NetIncomeLossPerOutstandingLimitedPartnershipUnitDiluted"),
        _t("NetIncomeLossPerOutstandingLimitedPartnershipUnitBasicAndDiluted"),
        _t("IncomeLossFromContinuingOperationsPerDilutedShare"),
        _t("IncomeLossFromContinuingOperationsPerOutstandingLimitedPartnershipUnitDiluted"),
        _t("DilutedEarningsLossPerShare", "ifrs-full"),
    ],
}
SHARE_FLOW_SPECS: dict[str, list[tuple]] = {
    "weighted_diluted_shares": [
        _t("WeightedAverageNumberOfDilutedSharesOutstanding"),
        _t("WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
        _t("WeightedAverageLimitedPartnershipUnitsOutstandingDiluted"),
        _t("AdjustedWeightedAverageShares", "ifrs-full"),
    ],
    "weighted_basic_shares": [
        _t("WeightedAverageNumberOfSharesOutstandingBasic"),
        _t("WeightedAverageLimitedPartnershipUnitsOutstandingBasic"),
        _t("WeightedAverageShares", "ifrs-full"),
    ],
}
INSTANT_SPECS: dict[str, list[tuple]] = {
    "cash": [
        _t("CashAndCashEquivalentsAtCarryingValue"), _t("CashAndCashEquivalents", "ifrs-full"),
        _t("CashAndDueFromBanks"), _t("Cash"),
        _t("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ],
    "short_term_investments": [
        _t("ShortTermInvestments"), _t("MarketableSecuritiesCurrent"),
        _t("AvailableForSaleSecuritiesDebtSecuritiesCurrent"), _t("AvailableForSaleSecuritiesCurrent"),
        _t("DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent"),
        _t("OtherShortTermInvestments"), _t("CurrentInvestments", "ifrs-full"),
    ],
    "cash_and_short_term_investments": [_t("CashCashEquivalentsAndShortTermInvestments")],
    "total_assets": [_t("Assets"), _t("Assets", "ifrs-full")],
    "total_liabilities": [_t("Liabilities"), _t("Liabilities", "ifrs-full")],
    "liabilities_and_equity": [_t("LiabilitiesAndStockholdersEquity"), _t("EquityAndLiabilities", "ifrs-full")],
    "equity_incl_nci": [
        _t("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        _t("PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest"), _t("Equity", "ifrs-full"),
    ],
    "minority_interest": [_t("MinorityInterest")],
    "current_assets": [_t("AssetsCurrent"), _t("CurrentAssets", "ifrs-full")],
    "current_liabilities": [_t("LiabilitiesCurrent"), _t("CurrentLiabilities", "ifrs-full")],
    "retained_earnings": [_t("RetainedEarningsAccumulatedDeficit"), _t("RetainedEarnings", "ifrs-full")],
    "stockholders_equity": [
        _t("StockholdersEquity"), _t("PartnersCapital"), _t("MembersEquity"),
        _t("EquityAttributableToOwnersOfParent", "ifrs-full"),
        _t("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), _t("Equity", "ifrs-full"),
    ],
}
_TEMP_EQUITY_TAGS = [
    "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
    "TemporaryEquityCarryingAmountAttributableToParent", "RedeemableNoncontrollingInterestEquityCarryingAmount",
    "RedeemableNoncontrollingInterestEquityCommonCarryingAmount",
    "RedeemableNoncontrollingInterestEquityPreferredCarryingAmount",
]
_BS_COMMON_SHARES = [_t("CommonStockSharesOutstanding"), _t("LimitedPartnersCapitalAccountUnitsOutstanding"),
                     _t("NumberOfSharesOutstanding", "ifrs-full")]

# Debt tags used by the tag-based (fallback / cross-check) total.
_DEBT_TOTAL_INCL_LEASES = ["DebtAndCapitalLeaseObligations"]
_DEBT_TOTAL = ["DebtLongtermAndShorttermCombinedAmount"]
_DEBT_NONCURRENT_TOTAL = ["LongTermDebtAndCapitalLeaseObligations", "LongTermDebtNoncurrent"]  # first present
_DEBT_CURRENT_LT = ["LongTermDebtAndCapitalLeaseObligationsCurrent", "LongTermDebtCurrent"]  # first present
_DEBT_SHORT_TERM = [["ShortTermBorrowings"], ["CommercialPaper"], ["LinesOfCreditCurrent"],
                    ["OtherShortTermBorrowings"], ["ShortTermBankLoansAndNotesPayable"], ["BankOverdrafts"]]
_DEBT_CURRENT_COMPONENTS = [["ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"], ["NotesPayableCurrent",
                            "NotesAndLoansPayableCurrent"], ["SecuredDebtCurrent"], ["UnsecuredDebtCurrent"],
                            ["SeniorNotesCurrent"], ["LoansPayableCurrent"]]
_DEBT_NONCURRENT_COMPONENTS = [
    ["ConvertibleNotesPayableNoncurrent", "ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable",
     "ConvertibleNotesPayable", "ConvertibleDebt"],
    ["SeniorLongTermNotes", "SeniorNotes"], ["LongTermNotesPayable", "NotesPayable", "LongTermNotesAndLoans"],
    ["LongTermLineOfCredit", "LineOfCredit"], ["SecuredLongTermDebt", "SecuredDebt"],
    ["UnsecuredLongTermDebt", "UnsecuredDebt"], ["SubordinatedLongTermDebt", "SubordinatedDebt"],
    ["OtherLongTermDebtNoncurrent", "OtherLongTermDebt"], ["LongTermLoansPayable", "LoansPayable"],
    ["JuniorSubordinatedNotes"],
]
_LEASE_INCLUSIVE = {"DebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligations",
                    "LongTermDebtAndCapitalLeaseObligationsCurrent"}
_ALL_DEBT_TAGS = sorted({t for grp in (_DEBT_SHORT_TERM + _DEBT_CURRENT_COMPONENTS + _DEBT_NONCURRENT_COMPONENTS)
                         for t in grp} | set(_DEBT_TOTAL_INCL_LEASES + _DEBT_TOTAL + _DEBT_NONCURRENT_TOTAL
                                             + _DEBT_CURRENT_LT)
                        | {"DebtCurrent", "LongTermDebt", "FinanceLeaseLiability", "FinanceLeaseLiabilityCurrent",
                           "FinanceLeaseLiabilityNoncurrent", "CapitalLeaseObligations",
                           "CapitalLeaseObligationsCurrent", "CapitalLeaseObligationsNoncurrent"})

# Balance-sheet-face debt rows (matched on the XBRL element name, checked against the label).
_FACE_DEBT_TAG = re.compile(
    r"Debt|Borrowing|NotesPayable|NotesAndLoansPayable|LoansPayable|SeniorNotes|SeniorLongTermNotes|SubordinatedNotes"
    r"|LongTermNotes|ConvertibleNotes|ConvertibleSeniorNotes|ConvertibleSubordinated|LineOfCredit|LinesOfCredit"
    r"|CreditFacilit|RevolvingCredit|Revolver|TermLoan|CommercialPaper|FinanceLeaseLiabilit|FinanceLeaseObligation"
    r"|CapitalLeaseObligation|Debenture|MortgageNotes|MortgagesPayable|MortgageLoansPayable|SecuredFinancing"
    r"|FinancingObligation|FinanceObligation|FederalHomeLoanBankAdvances|AdvancesFromFederalHomeLoanBanks"
    r"|BondsPayable|SecuritiesSoldUnderAgreementsToRepurchase|JuniorSubordinated|BankOverdraft"
    r"|CollateralizedLoanObligation|SecuritizedDebt")
_FACE_DEBT_TAG_EXCL = re.compile(
    r"OperatingLease|DebtSecurities|Receivable|InterestPayable|InterestAndDividendsPayable|AccruedInterest"
    r"|Derivative|Warrant|Embedded|DeferredFinanc|DeferredLoan|IssuanceCost|Abstract$|DeferredRevenue|Unamortized"
    r"|Guarantee|Extinguishment|^Liabilities(Current|Noncurrent)?$|^OtherLiabilities|AccountsPayable"
    r"|AccruedLiabilit|Deposit|^DueTo|^DueFrom|HeldForSale|DisposalGroup|DiscontinuedOperation|TaxLiabilit"
    r"|Pension|Postretirement|AssetRetirement")
_FACE_DEBT_LABEL = re.compile(
    r"debt|borrow|notes?\b|loans?\b|credit|revolv|commercial paper|finance leases?|capital leases?|debenture"
    r"|mortgage|financ|bonds?\b|advances|repurchase|convert|facilit", re.I)
_FACE_DEBT_LABEL_STRONG = re.compile(
    r"\b(debt|borrowings?|notes payable|senior notes|convertible (senior )?notes|term loans?|credit facilit\w*"
    r"|lines? of credit|revolv\w*|commercial paper|finance leases?|financ\w* obligations?|debentures"
    r"|mortgage notes?)\b", re.I)
_FACE_DEBT_LABEL_EXCL = re.compile(
    r"operating lease|accrued interest|interest payable|derivative|warrant|deferred revenue|unearned|held for sale"
    r"|discontinued|deferred (income )?tax|pension|accounts payable|customer|deposits", re.I)
_FACE_LEASE = re.compile(r"FinanceLease|CapitalLease", re.I)
_FACE_END = re.compile(r"^(StockholdersEquity|TemporaryEquity|RedeemableNoncontrollingInterest|PartnersCapital"
                       r"|MembersEquity|CommitmentsAndContingencies|LiabilitiesAndStockholdersEquity|Equity)")

_DA_LABEL = re.compile(r"depreciation|amorti[sz]ation|depletion", re.I)
_DA_LABEL_EXCL = re.compile(
    r"debt|financ|discount|premium|issuance|right[- ]of[- ]use|\blease|deferred|contract|commission|securit"
    r"|investment|content|bond|loan|stock|share|compensation|royalt|inventory|favou?rable|below|above|nuclear"
    r"|regulatory|turnaround|program|film|interest|tax|mortgage|servicing|goodwill impairment|gain|accretion of",
    re.I)


# --------------------------------------------------------------------------- small helpers
def _d(s: str) -> date:
    return date.fromisoformat(s)


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _is_periodic(form: str | None) -> bool:
    return bool(form) and form.upper().startswith(_PERIODIC)


def _is_annual(form: str | None) -> bool:
    return bool(form) and form.upper().startswith(_ANNUAL)


def _months_back(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    last = _calendar.monthrange(y, m)[1]
    day = last if d.day == _calendar.monthrange(d.year, d.month)[1] else min(d.day, last)
    return date(y, m, day)


_NUM_RE = re.compile(r"^\(?-?\s*[\d,]*\.?\d+\s*\)?$")
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_number(text) -> float | None:
    """Robust number parser for rendered statement cells: '$ 1,234', '(444)', '12.5%', '1.2B', 'N/A', '--'."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return None if isinstance(text, float) and math.isnan(text) else float(text)
    s = str(text).replace("\xa0", " ").strip()
    if not s or s.upper() in {"N/A", "NA", "-", "--", "—", "–", "NONE", "NM"}:
        return None
    s = re.sub(r"[$€£¥]|USD|CNY", "", s).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    mult = 1.0
    if s and s[-1].upper() in _SUFFIX and re.match(r"^-?[\d,]*\.?\d+$", s[:-1].strip()):
        mult = _SUFFIX[s[-1].upper()]
        s = s[:-1].strip()
    if not _NUM_RE.match(s):
        return None
    try:
        v = float(s.replace(",", "").replace(" ", "")) * mult
    except ValueError:
        return None
    return -abs(v) if neg else v


def _src(url: str) -> str:
    return f"{PROVIDER} {url}"


def _period_type(days: int | None) -> str | None:
    if days is None:
        return None
    for lo, hi, name in ((79, 104, "Q"), (168, 197, "H"), (258, 288, "9M"), (350, 380, "FY")):
        if lo <= days <= hi:
            return name
    return "other"


# --------------------------------------------------------------------------- ticker -> CIK
@lru_cache(maxsize=1)
def _ticker_table() -> tuple[dict, str]:
    obj, ts = fetch_json(TICKERS_URL, headers=_H)
    return {str(v["ticker"]).upper(): v for v in obj.values()}, ts


def edgar_symbol(ticker: str) -> str:
    """Pipeline symbol -> EDGAR convention: BRK.B / BRK/B -> BRK-B."""
    return ticker.strip().upper().replace(".", "-").replace("/", "-")


def cik_for(ticker: str) -> dict:
    """CIK lookup. Class shares resolve to the registrant (GEF.B -> GEF-B -> Greif CIK)."""
    out = {"ticker": ticker, "edgar_ticker": None, "cik": None, "cik10": None, "title": None,
           "source": _src(TICKERS_URL), "retrieved_at": None, "error": None}
    try:
        table, ts = _ticker_table()
    except FetchError as e:
        out["error"] = f"ticker table unavailable: {e}"
        return out
    out["retrieved_at"] = ts
    sym = edgar_symbol(ticker)
    for cand in (sym, sym.split("-")[0]):
        row = table.get(cand)
        if row:
            out.update(edgar_ticker=cand, cik=int(row["cik_str"]), cik10=f"{int(row['cik_str']):010d}",
                       title=row.get("title"))
            return out
    out["error"] = "ticker not found in SEC company_tickers.json"
    return out


@lru_cache(maxsize=256)
def _raw_facts(cik: int) -> tuple[dict | None, str, str | None, str | None]:
    url = FACTS_URL.format(cik=cik)
    try:
        obj, ts = fetch_json(url, headers=_H)
        return obj, url, ts, None
    except FetchError as e:
        return None, url, None, f"companyfacts unavailable ({e.status})"


@lru_cache(maxsize=256)
def _raw_subs(cik: int) -> tuple[dict | None, str, str | None, str | None]:
    url = SUBS_URL.format(cik=cik)
    try:
        obj, ts = fetch_json(url, headers=_H)
        return obj, url, ts, None
    except FetchError as e:
        return None, url, None, f"submissions unavailable ({e.status})"


def _filing_rows(block: dict, cik: int) -> list[dict]:
    n = len(block.get("accessionNumber", []))

    def col(name):
        v = block.get(name) or []
        return v if len(v) == n else [None] * n

    cols = {k: col(k) for k in ("accessionNumber", "form", "filingDate", "reportDate", "acceptanceDateTime",
                                "items", "primaryDocument", "primaryDocDescription", "isXBRL", "isInlineXBRL")}
    rows = []
    for i in range(n):
        accn = cols["accessionNumber"][i]
        doc = cols["primaryDocument"][i]
        rows.append({
            "accession": accn, "form": cols["form"][i], "filing_date": cols["filingDate"][i],
            "report_date": cols["reportDate"][i] or None, "accepted": cols["acceptanceDateTime"][i],
            "items": [x.strip() for x in (cols["items"][i] or "").split(",") if x.strip()],
            "primary_document": doc, "description": cols["primaryDocDescription"][i] or None,
            "is_xbrl": bool(cols["isXBRL"][i]), "is_inline_xbrl": bool(cols["isInlineXBRL"][i]),
            "url": ARCHIVE_URL.format(cik=cik, accn=accn.replace("-", ""), name=doc) if accn and doc else None,
        })
    return rows


@lru_cache(maxsize=256)
def _all_filings(cik: int, include_older: bool) -> tuple[list[dict], list[str]]:
    obj, _url, _ts, err = _raw_subs(cik)
    if obj is None:
        return [], [err or "submissions unavailable"]
    rows = _filing_rows(obj.get("filings", {}).get("recent", {}), cik)
    notes = []
    if include_older:
        for f in obj.get("filings", {}).get("files", []) or []:
            try:
                page, _ = fetch_json(SUBS_PAGE_URL.format(name=f["name"]), headers=_H)
                rows.extend(_filing_rows(page, cik))
            except FetchError as e:
                notes.append(f"older submissions page {f.get('name')} unavailable ({e.status})")
    rows.sort(key=lambda r: (r["filing_date"] or "", r["accession"] or ""), reverse=True)
    return rows, notes


# --------------------------------------------------------------------------- public: raw endpoints
def company_facts(ticker: str) -> dict:
    """Raw companyfacts JSON for the registrant (plus predecessor registrants, if mapped)."""
    info = cik_for(ticker)
    out = {"ticker": ticker, "cik": info["cik"], "entity_name": None, "facts": None, "taxonomies": {},
           "predecessors": [], "source": None, "retrieved_at": None, "error": info["error"]}
    if info["cik"] is None:
        return out
    obj, url, ts, err = _raw_facts(info["cik"])
    out.update(source=_src(url), retrieved_at=ts, error=err)
    if obj:
        out.update(entity_name=obj.get("entityName"), facts=obj.get("facts"),
                   taxonomies={k: len(v) for k, v in (obj.get("facts") or {}).items()})
    for pcik in PREDECESSOR_CIKS.get(edgar_symbol(ticker).split("-")[0], []):
        pobj, purl, pts, perr = _raw_facts(pcik)
        out["predecessors"].append({"cik": pcik, "entity_name": (pobj or {}).get("entityName"),
                                    "facts": (pobj or {}).get("facts"), "source": _src(purl),
                                    "retrieved_at": pts, "error": perr})
    return out


def submissions(ticker: str, forms: list[str] | tuple[str, ...] | None = None, since: str | None = None,
                limit: int | None = 200, include_older: bool = False) -> dict:
    """Filing list (most recent first) with form, filing/report dates, 8-K item numbers and document URL.

    forms: keep only these form types (exact match, e.g. ["8-K", "10-Q"]); since: ISO date lower bound on
    filing_date; include_older: also read the paginated history beyond EDGAR's ~1,000 most recent filings.
    """
    info = cik_for(ticker)
    out = {"ticker": ticker, "cik": info["cik"], "name": None, "sic": None, "sic_description": None,
           "fiscal_year_end": None, "exchanges": None, "tickers": None, "former_names": [], "state_of_inc": None,
           "category": None, "filings": [], "n_filings_total": 0, "notes": [], "source": None,
           "retrieved_at": None, "error": info["error"]}
    if info["cik"] is None:
        return out
    obj, url, ts, err = _raw_subs(info["cik"])
    out.update(source=_src(url), retrieved_at=ts, error=err)
    if obj is None:
        return out
    out.update(name=obj.get("name"), sic=obj.get("sic"), sic_description=obj.get("sicDescription"),
               fiscal_year_end=obj.get("fiscalYearEnd"), exchanges=obj.get("exchanges"), tickers=obj.get("tickers"),
               state_of_inc=obj.get("stateOfIncorporation"), category=obj.get("category"),
               former_names=[{"name": f.get("name"), "from": (f.get("from") or "")[:10] or None,
                              "to": (f.get("to") or "")[:10] or None} for f in obj.get("formerNames") or []])
    rows, notes = _all_filings(info["cik"], include_older)
    out["notes"] = list(notes)
    out["n_filings_total"] = len(rows)
    want = {f.upper() for f in forms} if forms else None
    sel = [dict(r) for r in rows if (not want or (r["form"] or "").upper() in want)
           and (not since or (r["filing_date"] or "") >= since)]
    out["filings"] = sel[:limit] if limit else sel
    return out


def recent_8k(ticker: str, since: str | None = None, limit: int = 40) -> dict:
    """8-K filings (date, item numbers, URL) -- catalysts / event screening."""
    sub = submissions(ticker, forms=["8-K", "8-K/A"], since=since, limit=limit)
    return {"ticker": ticker, "cik": sub["cik"], "filings": sub["filings"], "source": sub["source"],
            "retrieved_at": sub["retrieved_at"], "error": sub["error"]}


# --------------------------------------------------------------------------- EDGAR "R page" statements
def _parse_filing_summary(text: str) -> list[dict]:
    reps = []
    for m in re.finditer(r"<Report\b[^>]*>(.*?)</Report>", text, re.S):
        blk = m.group(1)

        def g(name):
            mm = re.search(rf"<{name}>(.*?)</{name}>", blk, re.S)
            return _html.unescape(mm.group(1).strip()) if mm else None

        reps.append({"short": g("ShortName") or "", "long": g("LongName") or "", "file": g("HtmlFileName"),
                     "cat": g("MenuCategory")})
    return reps


def _classify_report(rep: dict) -> str | None:
    name = rep["short"].lower()
    cat = (rep["cat"] or "").lower()
    if not cat and " - " in rep["long"]:
        cat = rep["long"].split(" - ")[1].strip().lower()
    if not rep["file"] or "parenthetical" in name:
        return None
    if cat in ("cover", "document"):
        return "cover"
    if cat not in ("statements", "statement"):
        return None
    if "cash flow" in name:
        return "CF"
    if re.search(r"balance sheet|financial position|financial condition|statements? of condition"
                 r"|assets and liabilities|assets, liabilities", name):
        return "BS"
    if re.search(r"equity|shareholders|stockholders|partners|capital|deficit|changes in", name) and not re.search(
            r"operations|income|earnings", name):
        return "EQ"
    if "comprehensive" in name and not re.search(
            r"operations|income statement|statements? of income|statements? of earnings|profit|loss and|income and"
            r"|earnings and|statements? of \(?loss", name):
        return "CI"
    if re.search(r"operations|income|earnings|profit|loss", name):
        return "IS"
    return "OTHER"


_HDR_DATE = re.compile(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}


def _hdr_date(text: str) -> date | None:
    m = _HDR_DATE.match(re.sub(r"\s+", " ", text.strip()))
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None


def _parse_r_table(html_text: str) -> dict | None:
    """Parse one rendered statement (R#.htm): scale, period columns and tagged rows."""
    soup = BeautifulSoup(html_text, "lxml")
    tbl = soup.find("table", class_="report")
    if tbl is None:
        return None
    trs = [tr for tr in tbl.find_all("tr") if tr.find_parent("table") is tbl]
    hdr, body_at = [], 0
    for i, tr in enumerate(trs):
        ths, tds = tr.find_all("th", recursive=False), tr.find_all("td", recursive=False)
        if ths and not tds:
            hdr.append(ths)
            body_at = i + 1
        else:
            break
    if not hdr or len(hdr[0]) < 2:
        return None
    title = hdr[0][0].get_text(" ", strip=True)
    top = []
    for th in hdr[0][1:]:
        cs = int(th.get("colspan") or 1)
        top.extend([(th.get_text(" ", strip=True), int(th.get("rowspan") or 1))] * cs)
    cols = []
    if len(hdr) > 1:
        bottom = iter(hdr[1])
        for text, rs in top:
            if rs >= 2:
                cols.append((None, text))
            else:
                b = next(bottom, None)
                cols.append((text, b.get_text(" ", strip=True) if b is not None else ""))
    else:
        cols = [(None, text) for text, _ in top]
    colspecs = []
    for toptxt, bottxt in cols:
        end = _hdr_date(bottxt)
        months = None
        if toptxt:
            mm = re.match(r"^(\d+)\s+Months?\s+Ended$", toptxt.strip(), re.I)
            months = int(mm.group(1)) if mm else None
            valid = end is not None and (mm is not None)
        else:
            valid = end is not None
        colspecs.append({"months": months, "end": end, "valid": valid})
    ms = re.search(r"(?:[$€£¥]|[A-Z]{3})\s+in\s+(Thousands|Millions|Billions)", title)
    ss = re.search(r"shares\s+in\s+(Thousands|Millions|Billions)", title, re.I)
    scale = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}
    cur = re.search(r"-\s*([A-Z]{3})\s*\(", title)
    rows = []
    for tr in trs[body_at:]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        a = tds[0].find("a", onclick=True)
        tax = tag = None
        if a is not None:
            mm = re.search(r"defref_([^']+)'", a.get("onclick", ""))
            if mm and "_" in mm.group(1):
                tax, tag = mm.group(1).rsplit("_", 1)
        cells = tds[1:]
        if len(cells) != len(colspecs):
            cells = [c for c in cells if "fn" not in (c.get("class") or [])]
            if len(cells) != len(colspecs):
                continue
        rows.append({"tax": tax, "tag": tag, "label": tds[0].get_text(" ", strip=True),
                     "vals": [parse_number(c.get_text(" ", strip=True)) for c in cells]})
    return {"title": title, "money_scale": scale[ms.group(1).lower()] if ms else 1.0,
            "share_scale": scale[ss.group(1).lower()] if ss else 1.0, "currency": cur.group(1) if cur else None,
            "cols": colspecs, "rows": rows}


def _row_scale(row: dict, tbl: dict) -> tuple[float, str]:
    """(multiplier, unit kind) for a statement row: money / per-share / shares."""
    tag, label = row.get("tag") or "", row.get("label") or ""
    if re.search(r"per share\)|per unit\)", label, re.I) or re.search(r"PerShare|PerUnit|EarningsLossPerShare", tag):
        return 1.0, "per_share"
    if "(in shares)" in label.lower() or re.search(r"WeightedAverageNumber|SharesOutstanding|SharesIssued", tag):
        return tbl["share_scale"], "shares"
    return tbl["money_scale"], "money"


@lru_cache(maxsize=8192)
def _filing_reports(cik: int, accn: str) -> tuple[tuple, str | None]:
    url = ARCHIVE_URL.format(cik=cik, accn=accn.replace("-", ""), name="FilingSummary.xml")
    try:
        res = fetch(url, headers=_H, max_age=None)
    except FetchError as e:
        return (), f"FilingSummary unavailable ({e.status})"
    reps = _parse_filing_summary(res["text"])
    out = []
    for r in reps:
        kind = _classify_report(r)
        if kind:
            out.append((kind, r["file"], r["short"]))
    return tuple(out), None


@lru_cache(maxsize=8192)
def _r_page(cik: int, accn: str, name: str) -> tuple[dict | None, str, str | None]:
    url = ARCHIVE_URL.format(cik=cik, accn=accn.replace("-", ""), name=name)
    try:
        res = fetch(url, headers=_H, max_age=None)
    except FetchError:
        return None, url, None
    try:
        return _parse_r_table(res["text"]), url, res["retrieved_at"]
    except Exception:  # malformed page: treat as unavailable rather than guess
        return None, url, res["retrieved_at"]


def filing_statement(cik: int, accn: str, kind: str) -> dict | None:
    """Parsed statement of one filing. kind: 'BS' | 'IS' | 'CF' | 'cover'. None when EDGAR has no R pages."""
    reps, _err = _filing_reports(cik, accn)
    names = [f for k, f, _ in reps if k == kind]
    if kind == "IS" and not names:
        names = [f for k, f, _ in reps if k == "CI"]
    for name in names[:3]:
        tbl, url, ts = _r_page(cik, accn, name)
        if not tbl:
            continue
        if kind == "BS" and not any(r["tag"] in ("Assets", "LiabilitiesAndStockholdersEquity", "EquityAndLiabilities",
                                                 "Liabilities") for r in tbl["rows"]):
            continue
        return {**tbl, "url": url, "retrieved_at": ts}
    return None


# --------------------------------------------------------------------------- company model
class _Company:
    """All facts of one registrant, a reporting calendar, and derived per-period values."""

    def __init__(self, ticker: str, prefer: str = "latest", use_pages: bool = True):
        self.ticker = ticker
        self.prefer = prefer
        self.use_pages = use_pages
        self.notes: list[str] = []
        self.info = cik_for(ticker)
        self.cik = self.info["cik"]
        self.error = self.info["error"]
        self.raws: list[tuple[int, dict]] = []
        self.extra: dict[tuple, list[dict]] = defaultdict(list)
        self._fc: dict[tuple, list[dict]] = {}
        self._memo: dict = {}
        self.facts_url = self.facts_ts = None
        self.entity_name = None
        self.sic = self.sic_desc = self.fye = None
        self.currency = None
        self.cal: list[dict] = []
        self.splits: list[dict] = []
        self.page_sources: set[str] = set()
        if self.cik is None:
            return
        obj, url, ts, err = _raw_facts(self.cik)
        self.facts_url, self.facts_ts = url, ts
        if obj is None:
            self.error = err
        else:
            self.entity_name = obj.get("entityName")
            self.raws.append((self.cik, obj))
        for pcik in PREDECESSOR_CIKS.get(edgar_symbol(ticker).split("-")[0], []):
            pobj, _purl, _pts, perr = _raw_facts(pcik)
            if pobj is None:
                self.notes.append(f"predecessor CIK {pcik}: {perr}")
            else:
                self.raws.append((pcik, pobj))
                self.notes.append(f"history before the current registrant is merged from predecessor CIK {pcik} "
                                  f"({pobj.get('entityName')})")
        sobj, _surl, _sts, serr = _raw_subs(self.cik)
        if sobj:
            self.sic = sobj.get("sic")
            self.sic_desc = sobj.get("sicDescription")
            self.fye = sobj.get("fiscalYearEnd")
            self.entity_name = self.entity_name or sobj.get("name")
        elif serr:
            self.notes.append(serr)
        if not self.raws:
            return
        self.currency = self._detect_currency()
        if use_pages:
            self._add_missing_filings()
        self._build_calendar()
        self._detect_splits()

    # ---- flags
    @property
    def is_financial(self) -> bool:
        try:
            s = int(self.sic or 0)
        except ValueError:
            return False
        return 6000 <= s <= 6399

    # ---- raw fact access
    def _detect_currency(self) -> str | None:
        best = Counter()
        for _cik, raw in self.raws[:1]:
            for tax in ("us-gaap", "ifrs-full"):
                node = raw.get("facts", {}).get(tax, {}).get("Assets")
                for unit, arr in (node or {}).get("units", {}).items():
                    best[unit] += len({x.get("end") for x in arr})
        return best.most_common(1)[0][0] if best else "USD"

    def unit_for(self, kind: str) -> str:
        return {"money": self.currency, "per_share": f"{self.currency}/shares", "shares": "shares"}[kind]

    def facts(self, tax: str, tag: str, unit: str) -> list[dict]:
        key = (tax, tag, unit)
        if key in self._fc:
            return self._fc[key]
        out = []
        for cik, raw in self.raws:
            node = raw.get("facts", {}).get(tax, {}).get(tag)
            if not node:
                continue
            for x in node.get("units", {}).get(unit, []):
                form = (x.get("form") or "").upper()
                if form.startswith(_EXCLUDED_FORMS) or x.get("val") is None:
                    continue
                try:
                    out.append({"start": _d(x["start"]) if x.get("start") else None, "end": _d(x["end"]),
                                "val": float(x["val"]), "accn": x.get("accn"), "form": x.get("form"),
                                "filed": _d(x["filed"]), "fy": x.get("fy"), "fp": x.get("fp"), "cik": cik,
                                "src": "api"})
                except (KeyError, ValueError, TypeError):
                    continue
        out.extend(self.extra.get(key, []))
        self._fc[key] = out
        return out

    def _pick(self, facts: list[dict]) -> dict:
        key = (lambda f: (f["filed"], f["accn"] or ""))
        return max(facts, key=key) if self.prefer == "latest" else min(facts, key=key)

    # ---- filings the companyfacts API does not carry: read their statements from the R pages
    def _add_missing_filings(self) -> None:
        rows, _ = _all_filings(self.cik, False)
        have = set()
        for _cik, raw in self.raws:
            for tax in ("us-gaap", "ifrs-full"):
                for unit_arr in ((raw.get("facts", {}).get(tax, {}).get("Assets") or {}).get("units", {})).values():
                    have.update(x.get("accn") for x in unit_arr)
        cutoff = (date.today() - timedelta(days=int(365.25 * 9))).isoformat()
        for r in rows:
            form = (r["form"] or "").upper()
            if not (_is_periodic(form) and r["is_xbrl"] and r["accession"] not in have
                    and (r["filing_date"] or "") >= cutoff):
                continue
            n = self._ingest_filing_pages(r)
            if n:
                self.notes.append(
                    f"{r['form']} filed {r['filing_date']} (period {r['report_date']}, {r['accession']}) is not in "
                    f"the companyfacts API; its statements were read from the filing's EDGAR R pages ({n} values)")
            elif form.startswith(("10-", "20-F", "40-F")):
                self.notes.append(
                    f"{r['form']} filed {r['filing_date']} (period {r['report_date']}) is not in the companyfacts "
                    f"API and its R pages could not be read -- period missing")

    def _ingest_filing_pages(self, row: dict) -> int:
        accn, form, filed = row["accession"], row["form"], _d(row["filing_date"])
        reps, _err = _filing_reports(self.cik, accn)
        if not any(k == "BS" for k, _, _ in reps):
            return 0
        fy = fp = None
        n = 0
        staged = []
        for kind, name, _short in reps:
            if kind in ("EQ", "OTHER"):
                continue
            tbl, url, _ts = _r_page(self.cik, accn, name)
            if not tbl:
                continue
            self.page_sources.add(url)
            for row_ in tbl["rows"]:
                if not row_["tag"]:
                    continue
                if kind == "cover":
                    txt = row_["label"].lower()
                    raw_vals = [v for v in row_["vals"] if v is not None]
                    if "fiscal year focus" in txt and raw_vals:
                        fy = int(raw_vals[0])
                    if row_["tag"] != "EntityCommonStockSharesOutstanding":
                        continue
                mult, kindu = _row_scale(row_, tbl)
                if kind == "cover":
                    mult, kindu = 1.0, "shares"
                for col, v in zip(tbl["cols"], row_["vals"]):
                    if v is None or col["end"] is None:
                        continue
                    if kind != "cover" and not col["valid"]:
                        continue
                    start = (_months_back(col["end"], col["months"]) + timedelta(days=1)) if col["months"] else None
                    staged.append((row_["tax"], row_["tag"], kindu, start, col["end"], v * mult, url, row_["label"]))
        if kind_fp := self._cover_fp(accn):
            fp = kind_fp
        for tax, tag, kindu, start, end, val, url, label in staged:
            unit = self.unit_for(kindu)
            self.extra[(tax, tag, unit)].append({
                "start": start, "end": end, "val": val, "accn": accn, "form": form, "filed": filed, "fy": fy,
                "fp": fp, "cik": self.cik, "src": "rpage", "url": url, "label": label})
            n += 1
        return n

    def _cover_fp(self, accn: str) -> str | None:
        tbl = filing_statement(self.cik, accn, "cover")
        if not tbl:
            return None
        html_like = [r for r in tbl["rows"] if "fiscal period focus" in r["label"].lower()]
        # the value is text (Q1/Q2/Q3/FY) which parse_number drops; re-read it from the raw page
        if not html_like:
            return None
        try:
            res = fetch(tbl["url"], headers=_H, max_age=None)
        except FetchError:
            return None
        m = re.search(r"Document Fiscal Period Focus.*?<td[^>]*>\s*(Q[1-4]|FY|H[12])\s*<", res["text"], re.S | re.I)
        return m.group(1).upper() if m else None

    # ---- reporting calendar: one entry per period end of a periodic report
    def _build_calendar(self) -> None:
        cur = self.unit_for("money")
        rep: dict[str, dict] = {}
        for tax in ("us-gaap", "ifrs-full"):
            for f in self.facts(tax, "Assets", cur):
                if f["start"] is not None or not _is_periodic(f["form"]) or not f["accn"]:
                    continue
                c = rep.get(f["accn"])
                if c is None or f["end"] > c["end"]:
                    rep[f["accn"]] = f
        by_end: dict[date, list[dict]] = defaultdict(list)
        for f in rep.values():
            by_end[f["end"]].append(f)
        entries = []
        for end in sorted(by_end):
            fl = sorted(by_end[end], key=lambda f: (f["filed"], f["accn"]))
            o = fl[0]
            entries.append({"end": end, "accn": o["accn"], "form": o["form"], "filed": o["filed"], "fy": o["fy"],
                            "fp": o["fp"], "cik": o["cik"], "annual": any(_is_annual(f["form"]) for f in fl),
                            "src": o["src"], "comparative_only": False,
                            "filings": [(f["accn"], f["form"], _iso(f["filed"])) for f in fl]})
        # periods that only exist as prior-period comparatives of the earliest XBRL filings
        if entries:
            first = entries[0]["end"]
            seen: dict[date, date] = {}
            for spec in FLOW_SPECS["net_income"] + FLOW_SPECS["revenue"]:
                if spec[0] != "T":
                    continue
                for f in self.facts(spec[1], spec[2], cur):
                    if f["start"] is None or f["end"] >= first:
                        continue
                    if _period_type((f["end"] - f["start"]).days + 1) in ("Q", "H", "9M", "FY"):
                        seen[f["end"]] = min(seen.get(f["end"], f["filed"]), f["filed"])
            extra = [{"end": e, "accn": None, "form": None, "filed": fd, "fy": None, "fp": None, "cik": self.cik,
                      "annual": False, "src": "api", "comparative_only": True, "filings": []}
                     for e, fd in seen.items()]
            entries = sorted(extra + entries, key=lambda x: x["end"])
        for i, e in enumerate(entries):
            e["start"] = entries[i - 1]["end"] + timedelta(days=1) if i else None
            e["days"] = (e["end"] - e["start"]).days + 1 if e["start"] else None
            e["ptype"] = _period_type(e["days"])
        # fiscal labels
        n_since_annual = None
        for i, e in enumerate(entries):
            if e["annual"]:
                n_since_annual = 0
            elif n_since_annual is not None:
                n_since_annual += 1
            e["pos"] = n_since_annual
        for i, e in enumerate(entries):
            fp = (e["fp"] or "").upper() or None
            if e["annual"]:
                fp = "FY"
            label = None
            if e["ptype"] == "Q":
                label = "Q4" if fp == "FY" else (fp if fp in ("Q1", "Q2", "Q3") else None)
                if label is None and e["pos"] in (1, 2, 3) and all(
                        entries[j]["ptype"] == "Q" for j in range(i - e["pos"] + 1, i + 1)):
                    label = f"Q{e['pos']}"
            elif e["ptype"] == "H":
                label = "H2" if fp == "FY" else ("H1" if fp in ("Q2", "H1") or e["pos"] == 1 else None)
            elif e["ptype"] == "FY":
                label = "FY" if fp == "FY" else None
            elif fp:
                label = fp if fp != "FY" else "FY-stub"
            e["label"] = label
            if e["fy"] is None:
                nxt = next((x for x in entries[i:] if x["annual"] and x["fy"] is not None), None)
                if nxt is not None and (nxt["end"] - e["end"]).days < 370:
                    e["fy"] = nxt["fy"]
        self.cal = entries
        self.cal_index = {e["end"]: i for i, e in enumerate(entries)}

    # ---- duration facts -> discrete periods
    def _dur_map(self, tax: str, tag: str, kind: str, adjust: bool = False) -> dict[tuple, dict]:
        key = ("dur", tax, tag, kind, adjust)
        if key in self._memo:
            return self._memo[key]
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for f in self.facts(tax, tag, self.unit_for(kind)):
            if f["start"] is not None and f["end"] >= f["start"]:
                groups[(f["start"], f["end"])].append(f)
        out = {}
        for k, fl in groups.items():
            f = dict(self._pick(fl))
            f["tag"] = f"{tax}:{tag}"
            f["raw"] = f["val"]
            if adjust:
                fac = self.split_factor(f["filed"])
                f["factor"] = fac
                f["val"] = f["val"] / fac if kind == "per_share" else f["val"] * fac
            out[k] = f
        self._memo[key] = out
        return out

    def _discrete_from(self, dur: dict[tuple, dict], additive: bool = True) -> dict[date, dict]:
        """Discrete value per calendar period from duration facts of ONE tag."""
        by_end: dict[date, list[tuple[date, dict]]] = defaultdict(list)
        for (s, e), f in dur.items():
            by_end[e].append((s, f))
        out: dict[date, dict] = {}
        cal = self.cal

        def cum(s0: date, j: int):
            ej = cal[j]["end"]
            for s, f in by_end.get(ej, ()):
                if abs((s - s0).days) <= _TOL.days:
                    return f["val"], [f]
            total, used, k = 0.0, [], j
            while k >= 0:
                o = out.get(cal[k]["end"])
                st = cal[k]["start"]
                if o is None or st is None:
                    return None
                total += o["val"]
                used.extend(o["facts"])
                if abs((st - s0).days) <= _TOL.days:
                    return total, used
                if st < s0:
                    return None
                k -= 1
            return None

        for i, ent in enumerate(cal):
            cands = by_end.get(ent["end"])
            if not cands:
                continue
            start = ent["start"]
            if start is None:
                # first calendar entry: accept a directly reported quarter only
                q = [f for s, f in cands if _period_type((ent["end"] - s).days + 1) == "Q"]
                if q:
                    out[ent["end"]] = {"val": q[0]["val"], "method": "direct", "facts": [q[0]]}
                continue
            direct = [f for s, f in cands if abs((s - start).days) <= _TOL.days]
            if direct:
                f = self._pick(direct)
                out[ent["end"]] = {"val": f["val"], "method": "direct", "facts": [f]}
                continue
            if not additive or i == 0:
                continue
            for s0, f in sorted(((s, f) for s, f in cands if s < start - _TOL), key=lambda x: x[0], reverse=True):
                c = cum(s0, i - 1)
                if c is not None:
                    out[ent["end"]] = {"val": f["val"] - c[0], "method": "ytd_diff", "facts": [f] + c[1]}
                    break
        return out

    def _spec_label(self, spec: tuple) -> str:
        if spec[0] == "T":
            return f"{spec[1]}:{spec[2]}"
        return spec[1]

    def _spec_discrete(self, spec: tuple, kind: str, adjust: bool, additive: bool) -> dict[date, dict]:
        key = ("disc", spec, kind, adjust, additive)
        if key in self._memo:
            return self._memo[key]
        if spec[0] == "T":
            out = self._discrete_from(self._dur_map(spec[1], spec[2], kind, adjust), additive)
        elif spec[0] == "SUM":
            parts = [self._spec_discrete(s, kind, adjust, additive) for s in spec[2]]
            out = {}
            for end in set(parts[0]).intersection(*parts[1:]) if parts else ():
                ps = [p[end] for p in parts]
                out[end] = {"val": sum(p["val"] for p in ps),
                            "method": "+".join(sorted({p["method"] for p in ps})),
                            "facts": [f for p in ps for f in p["facts"]]}
        elif spec[0] == "NEG":
            out = {end: {**v, "val": -v["val"]} for end, v in
                   self._spec_discrete(spec[2], kind, adjust, additive).items()}
        elif spec[0] == "CFROWS":
            out = self._discrete_from(self._cf_rows_dur(), additive) if self.use_pages else {}
        else:
            out = {}
        self._memo[key] = out
        return out

    def flow(self, concept: str, specs: list[tuple], kind: str = "money", adjust: bool = False,
             additive: bool = True, window_start: date | None = None) -> dict[date, dict]:
        """Per calendar period: first alternative with a value. Returns end -> {val, tag, method, ...}."""
        key = ("flow", concept, kind, adjust, additive)
        if key in self._memo:
            return self._memo[key]
        out: dict[date, dict] = {}
        ends = [e["end"] for e in self.cal]
        for spec in specs:
            missing = [e for e in ends if e not in out]
            if not missing:
                break
            if spec[0] == "CFROWS":
                # network-heavy fallback: only when recent periods are still unresolved
                recent = [e for e in missing if e >= (window_start or date.min)]
                if not recent:
                    continue
            disc = self._spec_discrete(spec, kind, adjust, additive)
            label = self._spec_label(spec)
            for e in missing:
                v = disc.get(e)
                if v is not None:
                    out[e] = {**v, "tag": label}
        self._memo[key] = out
        return out

    def fy_fact(self, specs: list[tuple], ent: dict, kind: str = "money", adjust: bool = False) -> dict | None:
        """Full-fiscal-year duration fact ending at an annual calendar entry (first alternative that has one)."""
        i = self.cal_index[ent["end"]]
        prev = next((self.cal[j] for j in range(i - 1, -1, -1) if self.cal[j]["annual"]), None)
        for spec in specs:
            if spec[0] != "T":
                continue
            best = None
            for (s, e), f in self._dur_map(spec[1], spec[2], kind, adjust).items():
                if e != ent["end"]:
                    continue
                days = (e - s).days + 1
                ok = abs((s - (prev["end"] + timedelta(days=1))).days) <= _TOL.days if prev else 350 <= days <= 380
                if ok and (best is None or days > best[0]):
                    best = (days, f)
            if best:
                return {**best[1], "days": best[0]}
        return None

    # ---- instants
    def _inst_map(self, tax: str, tag: str, kind: str = "money") -> dict[date, dict]:
        key = ("inst", tax, tag, kind)
        if key in self._memo:
            return self._memo[key]
        groups: dict[date, list[dict]] = defaultdict(list)
        for f in self.facts(tax, tag, self.unit_for(kind)):
            if f["start"] is None:
                groups[f["end"]].append(f)
        out = {e: {**self._pick(fl), "tag": f"{tax}:{tag}"} for e, fl in groups.items()}
        self._memo[key] = out
        return out

    def instant(self, specs: list[tuple], end: date, kind: str = "money") -> dict | None:
        for spec in specs:
            f = self._inst_map(spec[1], spec[2], kind).get(end)
            if f is not None:
                return f
        return None

    def tagval(self, tag: str, end: date) -> float | None:
        f = self._inst_map("us-gaap", tag).get(end)
        return f["val"] if f else None

    # ---- D&A from cash-flow statement rows (company-specific tags never reach the API)
    def _cf_rows_dur(self) -> dict[tuple, dict]:
        if "cfrows" in self._memo:
            return self._memo["cfrows"]
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for ent in self.cal:
            if not ent["accn"] or ent["end"] < date.today() - timedelta(days=int(365.25 * 10)):
                continue
            tbl = filing_statement(ent["cik"], ent["accn"], "CF")
            if not tbl:
                continue
            self.page_sources.add(tbl["url"])
            rows = []
            for r in tbl["rows"]:
                if r["tag"] and r["tag"].startswith("NetCashProvidedByUsedInOperatingActivities") and not r["tag"].endswith("Abstract"):
                    break
                if r["tag"] and not r["tag"].endswith("Abstract") and _DA_LABEL.search(r["label"]) \
                        and not _DA_LABEL_EXCL.search(r["label"]):
                    rows.append(r)
            if not rows:
                continue
            for ci, col in enumerate(tbl["cols"]):
                if not (col["valid"] and col["months"]):
                    continue
                vals = [(r, r["vals"][ci]) for r in rows if r["vals"][ci] is not None]
                if not vals:
                    continue
                start = _months_back(col["end"], col["months"]) + timedelta(days=1)
                groups[(start, col["end"])].append({
                    "start": start, "end": col["end"], "val": sum(v for _, v in vals) * tbl["money_scale"],
                    "accn": ent["accn"], "form": ent["form"], "filed": ent["filed"], "cik": ent["cik"],
                    "src": "rpage", "url": tbl["url"],
                    "tag": "CF rows: " + " + ".join(f"{r['tax']}:{r['tag']}" for r, _ in vals)})
        out = {}
        for k, fl in groups.items():
            f = dict(self._pick(fl))
            f["raw"] = f["val"]
            out[k] = f
        self._memo["cfrows"] = out
        return out

    # ---- debt
    def debt_from_tags(self, end: date) -> dict | None:
        v = {t: self.tagval(t, end) for t in _ALL_DEBT_TAGS}
        v = {t: x for t, x in v.items() if x is not None}
        if not v:
            return None
        comps: list[tuple[str, float]] = []
        lease_in = False

        def first(tags):
            for t in tags:
                if t in v:
                    return t
            return None

        tot = first(_DEBT_TOTAL_INCL_LEASES)
        if tot:
            comps.append((tot, v[tot]))
            lease_in = True
        elif first(_DEBT_TOTAL):
            comps.append((first(_DEBT_TOTAL), v[first(_DEBT_TOTAL)]))
        else:
            nonc, cur_lt = first(_DEBT_NONCURRENT_TOTAL), first(_DEBT_CURRENT_LT)
            if "DebtCurrent" in v:
                comps.append(("DebtCurrent", v["DebtCurrent"]))
                cur_done = True
            else:
                cur_done = False
                for grp in _DEBT_SHORT_TERM:
                    t = first(grp)
                    if t:
                        comps.append((t, v[t]))
            if nonc:
                comps.append((nonc, v[nonc]))
                if cur_lt and not cur_done:
                    comps.append((cur_lt, v[cur_lt]))
            elif "LongTermDebt" in v:
                # us-gaap definition: long-term debt INCLUDING the current portion
                comps.append(("LongTermDebt", v["LongTermDebt"]))
                if cur_done and cur_lt and v[cur_lt] <= v["LongTermDebt"]:
                    comps.append((f"less {cur_lt} (already in DebtCurrent)", -v[cur_lt]))
            else:
                if cur_lt and not cur_done:
                    comps.append((cur_lt, v[cur_lt]))
                elif not cur_done:
                    for grp in _DEBT_CURRENT_COMPONENTS:
                        t = first(grp)
                        if t:
                            comps.append((t, v[t]))
                for grp in _DEBT_NONCURRENT_COMPONENTS:
                    t = first(grp)
                    if t and not (t in ("LineOfCredit",) and "LinesOfCreditCurrent" in v
                                  and "LongTermLineOfCredit" not in v):
                        comps.append((t, v[t]))
            lease_in = any(t in _LEASE_INCLUSIVE for t, _ in comps)
        if not lease_in:
            comps.extend(self._lease_components(v))
        if not comps:
            return None
        return {"total": sum(x for _, x in comps), "components": [{"tag": f"us-gaap:{t}" if not t.startswith("less") else t,
                                                                  "value": x} for t, x in comps]}

    @staticmethod
    def _lease_components(v: dict) -> list[tuple[str, float]]:
        if "FinanceLeaseLiabilityCurrent" in v or "FinanceLeaseLiabilityNoncurrent" in v:
            return [(t, v[t]) for t in ("FinanceLeaseLiabilityCurrent", "FinanceLeaseLiabilityNoncurrent") if t in v]
        if "FinanceLeaseLiability" in v:
            return [("FinanceLeaseLiability", v["FinanceLeaseLiability"])]
        if "CapitalLeaseObligationsCurrent" in v or "CapitalLeaseObligationsNoncurrent" in v:
            return [(t, v[t]) for t in ("CapitalLeaseObligationsCurrent", "CapitalLeaseObligationsNoncurrent")
                    if t in v]
        if "CapitalLeaseObligations" in v:
            return [("CapitalLeaseObligations", v["CapitalLeaseObligations"])]
        return []

    def debt_from_face(self, ent: dict) -> dict | None:
        """Sum of the debt line items on the face of the balance sheet of the period's own filing."""
        if not (self.use_pages and ent.get("accn")):
            return None
        tbl = filing_statement(ent["cik"], ent["accn"], "BS")
        if not tbl:
            return None
        ci = next((i for i, c in enumerate(tbl["cols"]) if c["valid"] and c["months"] is None
                   and c["end"] == ent["end"]), None)
        if ci is None:
            return None
        self.page_sources.add(tbl["url"])
        in_liab, n_liab, comps, flags = False, 0, [], []
        for r in tbl["rows"]:
            tag = r["tag"] or ""
            if not in_liab:
                if tag in ("Assets",) or re.match(r"^Liabilities.*Abstract$", tag):
                    in_liab = True
                continue
            if _FACE_END.match(tag):
                break
            val = r["vals"][ci]
            if tag.endswith("Abstract"):
                continue
            n_liab += 1
            if tag == "Liabilities":
                break
            std = r["tax"] in ("us-gaap", "ifrs-full")
            hit = bool(_FACE_DEBT_TAG.search(tag)) and not _FACE_DEBT_TAG_EXCL.search(tag) \
                and bool(_FACE_DEBT_LABEL.search(r["label"]))
            if not hit and not std:
                hit = bool(_FACE_DEBT_LABEL_STRONG.search(r["label"])) and not _FACE_DEBT_TAG_EXCL.search(tag)
            if not hit or _FACE_DEBT_LABEL_EXCL.search(r["label"]) or val is None:
                continue
            val *= tbl["money_scale"]
            if val < 0:
                flags.append(f"negative debt row ignored: {r['label']}")
                continue
            if len(comps) >= 2 and val > 0 and re.match(r"^total\b", r["label"], re.I):
                run = 0.0
                for c in reversed(comps):
                    run += c["value"]
                    if abs(run - val) <= 0.005 * val:
                        break
                else:
                    run = None
                if run is not None:
                    continue  # a subtotal of the rows above
            comps.append({"tag": f"{r['tax']}:{tag}", "label": r["label"], "value": val})
        if n_liab < 3:
            return None
        tags = {c["tag"].split(":")[1] for c in comps}
        if "LongTermDebt" in tags and ("LongTermDebtNoncurrent" in tags or "LongTermDebtAndCapitalLeaseObligations" in tags):
            flags.append("face shows both LongTermDebt and its non-current portion; possible double count")
        if not any(_FACE_LEASE.search(c["tag"]) for c in comps):
            v = {t: self.tagval(t, ent["end"]) for t in ("FinanceLeaseLiability", "FinanceLeaseLiabilityCurrent",
                                                         "FinanceLeaseLiabilityNoncurrent")}
            for t, x in self._lease_components({t: x for t, x in v.items() if x is not None}):
                comps.append({"tag": f"us-gaap:{t}", "label": "finance lease liability (XBRL tag, not on the face)",
                              "value": x})
        return {"total": sum(c["value"] for c in comps), "components": comps, "url": tbl["url"], "flags": flags}

    def total_debt(self, ent: dict) -> dict:
        key = ("debt", ent["end"])
        if key in self._memo:
            return self._memo[key]
        tags = self.debt_from_tags(ent["end"])
        face = self.debt_from_face(ent)
        out = {"value": None, "method": None, "components": [], "tag_based": tags["total"] if tags else None,
               "face_based": face["total"] if face else None, "agree": None, "flags": []}
        if face is not None and tags is not None:
            hi = max(abs(face["total"]), abs(tags["total"]))
            out["agree"] = bool(hi == 0 or abs(face["total"] - tags["total"]) <= 0.02 * hi)
        if face is not None and (face["total"] > 0 or tags is None or tags["total"] == 0):
            out.update(value=face["total"], components=face["components"], flags=face["flags"],
                       method="sum of debt line items on the balance-sheet face of the period's filing "
                              "(+ finance-lease liabilities from XBRL tags when not on the face)",
                       url=face["url"])
            if not face["components"]:
                out["method"] = "no debt line item on the balance-sheet face and no debt XBRL tag -> 0"
        elif tags is not None:
            out.update(value=tags["total"], components=tags["components"],
                       method="XBRL tag hierarchy (balance-sheet face "
                              + ("shows no debt line; debt tags only in notes" if face is not None else "not available")
                              + ")")
        self._memo[key] = out
        return out

    # ---- shares outstanding (cover page)
    def cover_shares(self, ent: dict) -> dict | None:
        if "dei" not in self._memo:
            by_accn: dict[str, dict] = {}
            for f in self.facts("dei", "EntityCommonStockSharesOutstanding", "shares"):
                if f["accn"] and (f["accn"] not in by_accn or f["end"] > by_accn[f["accn"]]["end"]):
                    by_accn[f["accn"]] = f
            self._memo["dei"] = by_accn
        for accn, _form, _filed in ent.get("filings") or []:
            f = self._memo["dei"].get(accn)
            if f is not None and f["val"] > 0:
                return {**f, "tag": "dei:EntityCommonStockSharesOutstanding"}
        return None

    # ---- stock splits
    def _detect_splits(self) -> None:
        cands = []
        eps_maps = [self.facts(s[1], s[2], self.unit_for("per_share")) for s in PER_SHARE_SPECS["eps_diluted"]
                    if s[0] == "T"]
        eps_by: dict[tuple, dict[date, float]] = defaultdict(dict)
        for arr in eps_maps:
            for f in arr:
                if f["start"] is not None:
                    eps_by[(f["start"], f["end"])].setdefault(f["filed"], f["val"])
        for specs in (SHARE_FLOW_SPECS["weighted_diluted_shares"], SHARE_FLOW_SPECS["weighted_basic_shares"]):
            for spec in specs:
                groups: dict[tuple, dict[date, float]] = defaultdict(dict)
                for f in self.facts(spec[1], spec[2], "shares"):
                    if f["start"] is not None and f["val"] > 0:
                        groups[(f["start"], f["end"])].setdefault(f["filed"], f["val"])
                for per, by_filed in groups.items():
                    seq = sorted(by_filed.items())
                    for (f0, v0), (f1, v1) in zip(seq, seq[1:]):
                        r = v1 / v0
                        if abs(math.log(r)) < math.log(1.15) or r > 200 or r < 1 / 200:
                            continue
                        e0, e1 = eps_by.get(per, {}).get(f0), eps_by.get(per, {}).get(f1)
                        if e0 and e1:
                            er = e0 / e1
                            if er <= 0 or abs(math.log(er / r)) > math.log(1.25):
                                continue  # EPS was not restated by the same ratio -> not a split
                            corroborated = True
                        else:
                            corroborated = False
                        cands.append({"lo": f0, "hi": f1, "ratio": r, "eps": corroborated})
        events: list[dict] = []
        conflicts = 0
        for c in sorted(cands, key=lambda c: (c["hi"] - c["lo"]).days):
            over = [e for e in events if e["lo"] < c["hi"] and c["lo"] < e["hi"]]
            if not over:
                events.append({"lo": c["lo"], "hi": c["hi"], "ratios": [c["ratio"]], "eps": c["eps"]})
                continue
            prod = math.prod(_median(e["ratios"]) for e in over)
            if abs(math.log(prod / c["ratio"])) <= math.log(1.06):
                if len(over) == 1:
                    e = over[0]
                    e["lo"], e["hi"] = max(e["lo"], c["lo"]), min(e["hi"], c["hi"])
                    e["ratios"].append(c["ratio"])
                    e["eps"] = e["eps"] or c["eps"]
            else:
                conflicts += 1
        out = []
        for e in sorted(events, key=lambda e: e["hi"]):
            r = _median(e["ratios"])
            nice = _nice_ratio(r)
            if len(e["ratios"]) < 2 and not (e["eps"] and nice):
                continue  # a single unconfirmed restatement is not enough evidence
            if not e["eps"] and not nice:
                continue
            ratio = nice or r
            out.append({
                "ratio": round(ratio, 6), "kind": ("stock split" if ratio > 1 else "reverse split") if nice
                else "share-basis restatement (recapitalisation / exchange ratio)",
                "last_filing_on_old_basis": _iso(e["lo"]), "first_filing_on_new_basis": _iso(e["hi"]),
                "observations": len(e["ratios"]), "eps_corroborated": bool(e["eps"]),
                "_hi": e["hi"]})
        self.splits = out
        if conflicts:
            self.notes.append(f"{conflicts} restated share-count observation(s) did not fit a clean split pattern "
                              "and were ignored")

    def split_factor(self, filed: date) -> float:
        """Multiplier that puts a SHARE COUNT from a filing made on `filed` onto the current basis
        (divide per-share values by it)."""
        fac = 1.0
        for s in self.splits:
            if filed < s["_hi"]:
                fac *= s["ratio"]
        return fac


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _nice_ratio(r: float) -> float | None:
    for num in (1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100, 1.25):
        for cand in (num, 1.0 / num):
            if abs(r / cand - 1) <= 0.015:
                return cand
    return None


@lru_cache(maxsize=128)
def _company(ticker: str, prefer: str = "latest", use_pages: bool = True) -> _Company:
    return _Company(ticker, prefer, use_pages)


# --------------------------------------------------------------------------- per-period assembly
_FLOW_FIELDS = ("revenue", "gross_profit", "operating_income", "ebit", "depreciation_amortization", "ebitda",
                "net_income", "interest_expense", "operating_cash_flow", "capex", "free_cash_flow")


@lru_cache(maxsize=128)
def _periods(ticker: str, years: int, prefer: str, use_pages: bool) -> tuple[_Company, list[dict]]:
    co = _company(ticker, prefer, use_pages)
    if not co.cal:
        return co, []
    wstart = date.today() - timedelta(days=int(365.25 * years) + 5)
    fl = {c: co.flow(c, specs, window_start=wstart) for c, specs in FLOW_SPECS.items()}
    eps = co.flow("eps_diluted", PER_SHARE_SPECS["eps_diluted"], kind="per_share", adjust=True)
    wsh = co.flow("weighted_diluted_shares", SHARE_FLOW_SPECS["weighted_diluted_shares"], kind="shares",
                  adjust=True, additive=False)
    src = _src(co.facts_url)
    recs = []
    for ent in co.cal:
        end = ent["end"]
        tags: dict[str, str] = {}

        def fv(concept, store=None, _end=end, _tags=tags):
            x = (store or fl[concept]).get(_end)
            if x is None:
                return None
            _tags[concept] = f"{x['tag']} [{x['method']}]"
            return x["val"]

        def iv(concept, kind="money", _end=end, _tags=tags):
            f = co.instant(INSTANT_SPECS[concept], _end, kind)
            if f is None:
                return None
            _tags[concept] = f["tag"]
            return f["val"]

        rec = {
            "ticker": ticker, "cik": co.cik, "period_end": _iso(end), "period_start": _iso(ent["start"]),
            "period_days": ent["days"], "period_type": ent["ptype"], "fiscal_year": ent["fy"],
            "fiscal_period": ent["label"], "is_fiscal_year_end": bool(ent["annual"]), "form": ent["form"],
            "accession": ent["accn"], "filed": _iso(ent["filed"]),
            "filed_is_later_comparative": bool(ent["comparative_only"]), "currency": co.currency,
        }
        rev = fv("revenue")
        cor = fv("cost_of_revenue")
        gp = fv("gross_profit")
        if gp is None and rev is not None and cor is not None:
            gp = rev - cor
            tags["gross_profit"] = f"derived: revenue - cost of revenue ({tags['cost_of_revenue'].split(' [')[0]})"
        oi = fv("operating_income")
        pti = fv("pretax_income")
        ie = fv("interest_expense")
        da = fv("depreciation_amortization")
        ebit = oi
        ebit_method = tags.get("operating_income")
        if ebit is None and pti is not None and ie is not None and not co.is_financial:
            ebit = pti + ie
            ebit_method = ("PROXY: pre-tax income + interest expense (no OperatingIncomeLoss tag; includes "
                           "non-operating items) = " + tags["pretax_income"].split(" [")[0] + " + "
                           + tags["interest_expense"].split(" [")[0])
        if ebit is not None:
            tags["ebit"] = ebit_method
        ebitda = ebit + da if (ebit is not None and da is not None and not co.is_financial) else None
        if ebitda is not None:
            tags["ebitda"] = "ebit + depreciation_amortization"
        ni = fv("net_income")
        cfo = fv("operating_cash_flow")
        capex = fv("capex")
        csw = fv("capitalized_software")
        fcf = None
        if cfo is not None and capex is not None:
            fcf = cfo - capex - (csw or 0.0)
            tags["free_cash_flow"] = "operating_cash_flow - capex" + (" - capitalized_software" if csw else "")
        e = eps.get(end)
        w = wsh.get(end)
        if e is not None:
            tags["eps_diluted"] = f"{e['tag']} [{e['method']}]" + (
                "; APPROXIMATION: fiscal-year EPS minus earlier periods (weighted share counts differ)"
                if e["method"] != "direct" else "")
        if w is not None:
            tags["weighted_diluted_shares"] = f"{w['tag']} [{w['method']}]"
        rec.update({
            "revenue": rev, "gross_profit": gp, "operating_income": oi, "ebit": ebit,
            "depreciation_amortization": da, "ebitda": ebitda, "net_income": ni,
            "eps_diluted": round(e["val"], 6) if e is not None else None,
            "eps_diluted_as_reported": e["facts"][0]["raw"] if e is not None and e["method"] == "direct" else None,
            "interest_expense": ie, "operating_cash_flow": cfo, "capex": capex, "capitalized_software": csw,
            "free_cash_flow": fcf, "pretax_income": pti,
        })
        cash = iv("cash")
        sti = iv("short_term_investments")
        if sti is None and cash is not None:
            both = co.instant(INSTANT_SPECS["cash_and_short_term_investments"], end)
            if both is not None and "RestrictedCash" not in tags.get("cash", ""):
                sti = both["val"] - cash
                tags["short_term_investments"] = "derived: us-gaap:CashCashEquivalentsAndShortTermInvestments - cash"
        ta = iv("total_assets")
        tl = iv("total_liabilities")
        if tl is None:
            lse = co.instant(INSTANT_SPECS["liabilities_and_equity"], end)
            eq = co.instant(INSTANT_SPECS["equity_incl_nci"], end)
            eq_val = eq["val"] if eq else None
            eq_tag = eq["tag"] if eq else None
            if eq_val is None:
                se = co.instant([_t("StockholdersEquity"), _t("PartnersCapital")], end)
                if se is not None:
                    mi = co.instant(INSTANT_SPECS["minority_interest"], end)
                    eq_val = se["val"] + (mi["val"] if mi else 0.0)
                    eq_tag = se["tag"] + (" + us-gaap:MinorityInterest" if mi else "")
            if lse is not None and eq_val is not None:
                temp = [(t, co.tagval(t, end)) for t in _TEMP_EQUITY_TAGS]
                temp = [(t, x) for t, x in temp if x is not None]
                tl = lse["val"] - eq_val - (temp[0][1] if temp else 0.0)
                tags["total_liabilities"] = (f"derived: {lse['tag']} - {eq_tag}"
                                             + (f" - us-gaap:{temp[0][0]}" if temp else "")
                                             + " (no Liabilities tag)")
        debt = co.total_debt(ent)
        if debt["value"] is not None:
            tags["total_debt"] = " + ".join(c["tag"] for c in debt["components"]) or debt["method"]
        sh = co.cover_shares(ent)
        sh_raw = sh_adj = sh_asof = None
        if sh is not None:
            fac = co.split_factor(sh["filed"])
            sh_raw, sh_adj, sh_asof = sh["val"], sh["val"] * fac, sh["end"]
            tags["shares_outstanding"] = sh["tag"] + " (filing cover page)"
        else:
            bs = co.instant(_BS_COMMON_SHARES, end, "shares")
            if bs is not None and bs["val"] > 0:
                fac = co.split_factor(bs["filed"])
                sh_raw, sh_adj, sh_asof = bs["val"], bs["val"] * fac, end
                tags["shares_outstanding"] = bs["tag"] + " (balance sheet; cover-page count not in the API)"
        rec.update({
            "cash": cash, "short_term_investments": sti, "total_debt": debt["value"],
            "total_debt_method": debt["method"], "total_debt_components": debt["components"],
            "total_debt_tag_based": debt["tag_based"], "total_debt_face_based": debt["face_based"],
            "total_debt_methods_agree": debt["agree"], "total_debt_flags": debt["flags"],
            "total_assets": ta, "total_liabilities": tl, "current_assets": iv("current_assets"),
            "current_liabilities": iv("current_liabilities"), "retained_earnings": iv("retained_earnings"),
            "stockholders_equity": iv("stockholders_equity"),
            "shares_outstanding": sh_adj, "shares_outstanding_as_reported": sh_raw,
            "shares_outstanding_as_of": _iso(sh_asof),
            "weighted_diluted_shares": w["val"] if w is not None else None,
            "weighted_diluted_shares_as_reported": w["facts"][0]["raw"] if w is not None else None,
            "tags": tags, "source": src, "retrieved_at": co.facts_ts,
        })
        recs.append(rec)
    # trailing-twelve-month sums
    for i, rec in enumerate(recs):
        ttm: dict[str, float | None] = {}
        days, j, chain = 0, i, []
        while j >= 0 and days < 350 and recs[j]["period_days"]:
            days += recs[j]["period_days"]
            chain.append(recs[j])
            j -= 1
        ok = 350 <= days <= 380
        for f in _FLOW_FIELDS:
            vals = [r[f] for r in chain]
            ttm[f] = sum(vals) if ok and all(v is not None for v in vals) else None
        e_vals = [r["eps_diluted"] for r in chain]
        ttm["eps_diluted"] = round(sum(e_vals), 6) if ok and all(v is not None for v in e_vals) else None
        rec["_ttm"] = ttm
        rec["_ttm_days"] = days if ok else None
        rec["_ttm_periods"] = len(chain) if ok else None
    return co, recs


def _window(recs: list[dict], years: int) -> list[dict]:
    lo = (date.today() - timedelta(days=int(365.25 * years) + 5)).isoformat()
    return [r for r in recs if r["period_end"] >= lo]


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def _base(co: _Company, ticker: str) -> dict:
    cal = [e for e in co.cal if not e["comparative_only"]]
    forms = Counter((e["form"] or "").split("/")[0] for e in cal)
    types = Counter(e["ptype"] for e in cal[-12:])
    periodicity = None
    if types:
        periodicity = {"Q": "quarterly", "H": "semi-annual", "FY": "annual"}.get(types.most_common(1)[0][0], "irregular")
    return {
        "ticker": ticker, "cik": co.cik, "entity_name": co.entity_name, "sic": co.sic,
        "sic_description": co.sic_desc, "fiscal_year_end_mmdd": co.fye, "currency": co.currency,
        "periodicity": periodicity, "report_forms": dict(forms),
        "is_financial_sector": co.is_financial,
        "splits": [{k: v for k, v in s.items() if not k.startswith("_")} for s in co.splits],
        "source": _src(co.facts_url) if co.facts_url else None, "retrieved_at": co.facts_ts,
        "error": co.error,
    }


def _fy_checks(co: _Company, recs: list[dict]) -> list[dict]:
    """Sum of the discrete periods of a fiscal year vs the reported full-year fact (restatement detector)."""
    out = []
    by_end = {r["period_end"]: r for r in recs}
    for ent in co.cal:
        if not ent["annual"] or ent["ptype"] not in ("Q", "H"):
            continue
        i = co.cal_index[ent["end"]]
        chain, days, j = [], 0, i
        while j >= 0 and days < 350 and co.cal[j]["days"]:
            days += co.cal[j]["days"]
            chain.append(by_end.get(_iso(co.cal[j]["end"])))
            j -= 1
        if not (350 <= days <= 380) or any(c is None for c in chain):
            continue
        for concept in ("revenue", "operating_income", "net_income"):
            fy = co.fy_fact(FLOW_SPECS[concept], ent)
            vals = [c[concept] for c in chain]
            if fy is None or any(v is None for v in vals) or not fy["val"]:
                continue
            diff = sum(vals) / fy["val"] - 1
            if abs(diff) > 0.01:
                out.append({"fiscal_year_end": _iso(ent["end"]), "concept": concept, "sum_of_periods": sum(vals),
                            "fiscal_year_fact": fy["val"], "diff_pct": round(100 * diff, 2),
                            "note": "periods were only partly restated (e.g. discontinued operations); "
                                    "TTM sums spanning this year mix bases"})
    return out


# --------------------------------------------------------------------------- public: fundamentals
def quarterly_fundamentals(ticker: str, years: int = 8, prefer: str = "latest", use_filing_pages: bool = True) -> dict:
    """One record per reported period end (fiscal quarter; half-year / year for filers that only file those).

    Flow items are DISCRETE for the period (Q2 = 6M - Q1, Q4 = FY - 9M). Balance-sheet items are at period_end.
    `tags` in each record names the XBRL element and derivation behind every number.
    """
    co, recs = _periods(ticker, years, prefer, use_filing_pages)
    out = _base(co, ticker)
    win = [_public(r) for r in _window(recs, years)]
    used: dict[str, Counter] = defaultdict(Counter)
    for r in win:
        for c, t in r["tags"].items():
            used[c][re.sub(r" \[.*", "", t)] += 1
    out.update({
        "prefer": prefer, "n_periods": len(win),
        "first_period_end": win[0]["period_end"] if win else None,
        "last_period_end": win[-1]["period_end"] if win else None,
        "tags_used": {c: dict(v.most_common()) for c, v in used.items()},
        "fiscal_year_sum_checks": _fy_checks(co, recs),
        "notes": list(co.notes) + _standing_notes(co, win),
        "filing_page_sources": sorted(co.page_sources)[:6],
        "quarters": win,
    })
    return out


def _standing_notes(co: _Company, win: list[dict]) -> list[str]:
    notes = []
    if co.is_financial:
        notes.append("financial-sector registrant (SIC 6000-6399): EBIT/EBITDA are not computed (interest is an "
                     "operating cost) and 'total debt' is funding, not leverage")
    types = Counter(r["period_type"] for r in win)
    if win and types.get("Q", 0) < 0.6 * len(win):
        notes.append("discrete QUARTERS are not available from XBRL for most periods (foreign private issuer or "
                     f"missing interim XBRL): period types in window = {dict(types)}")
    if co.currency and co.currency != "USD":
        notes.append(f"reporting currency is {co.currency}; all money values and per-share values are in "
                     f"{co.currency} (per ordinary share, not per ADS)")
    if win and win[-1]["period_end"] < (date.today() - timedelta(days=200)).isoformat():
        notes.append(f"latest period in EDGAR XBRL is {win[-1]['period_end']} (stale > 200 days)")
    if any(r["tags"].get("ebit", "").startswith("PROXY") for r in win):
        notes.append("EBIT is a PROXY (pre-tax income + interest expense) for some/all periods because the filer "
                     "does not tag operating income")
    return notes


def ttm_series(ticker: str, years: int = 8, prefer: str = "latest", use_filing_pages: bool = True) -> dict:
    """Trailing-twelve-month sums at each period end + balance-sheet values + the public (filing) date.

    Record keys line up with screener.evebitda.build_monthly_series: period_end, filed, ebitda_ttm, total_debt,
    cash, shares, shares_as_of.  `shares` is ALREADY on the current split basis (pass split_events=None).
    """
    co, recs = _periods(ticker, years, prefer, use_filing_pages)
    out = _base(co, ticker)
    rows = []
    by_end = {r["period_end"]: r for r in recs}
    for r in _window(recs, years):
        t = r["_ttm"]
        prior = None
        if r["_ttm_days"]:
            target = date.fromisoformat(r["period_end"]) - timedelta(days=365)
            near = min(recs, key=lambda x: abs((date.fromisoformat(x["period_end"]) - target).days))
            if abs((date.fromisoformat(near["period_end"]) - target).days) <= 10:
                prior = by_end[near["period_end"]]["_ttm"].get("ebitda")
        growth = (t["ebitda"] / prior - 1) if (t.get("ebitda") is not None and prior and prior > 0) else None
        rows.append({
            "ticker": ticker, "period_end": r["period_end"], "fiscal_year": r["fiscal_year"],
            "fiscal_period": r["fiscal_period"], "period_type": r["period_type"], "form": r["form"],
            "filed": r["filed"], "filed_is_later_comparative": r["filed_is_later_comparative"],
            "ttm_days": r["_ttm_days"], "ttm_periods": r["_ttm_periods"],
            "revenue_ttm": t["revenue"], "ebitda_ttm": t["ebitda"], "ebit_ttm": t["ebit"],
            "depreciation_amortization_ttm": t["depreciation_amortization"],
            "fcf_ttm": t["free_cash_flow"], "operating_cash_flow_ttm": t["operating_cash_flow"],
            "capex_ttm": t["capex"], "net_income_ttm": t["net_income"],
            "interest_expense_ttm": t["interest_expense"], "eps_diluted_ttm": t["eps_diluted"],
            "ebitda_ttm_prior_year": prior, "ebitda_ttm_yoy_growth": growth,
            "total_debt": r["total_debt"], "total_debt_methods_agree": r["total_debt_methods_agree"],
            "cash": r["cash"], "short_term_investments": r["short_term_investments"],
            "shares": r["shares_outstanding"], "shares_as_reported": r["shares_outstanding_as_reported"],
            "shares_as_of": r["shares_outstanding_as_of"],
            "ebit_basis": r["tags"].get("ebit"), "da_basis": r["tags"].get("depreciation_amortization"),
            "currency": r["currency"], "source": r["source"], "retrieved_at": r["retrieved_at"],
        })
    out.update({
        "prefer": prefer, "n_periods": len(rows),
        "n_with_ebitda_ttm": sum(1 for x in rows if x["ebitda_ttm"] is not None),
        "definition": "TTM = sum of the discrete reported periods covering the trailing 350-380 days "
                      "(4 quarters, 2 half-years or 1 fiscal year); None if any component is missing or the "
                      "periods do not tile 12 months (e.g. a fiscal-year-end change)",
        "notes": list(co.notes) + _standing_notes(co, [_public(r) for r in _window(recs, years)]),
        "fiscal_year_sum_checks": _fy_checks(co, recs),
        "quarters": rows,
    })
    return out


def annual_eps(ticker: str, years: int = 6, use_filing_pages: bool = True) -> dict:
    """Diluted EPS for the last `years` fiscal years on the CURRENT share basis.

    Each year uses the most recently filed value (the latest 10-K restates the two prior years); values taken
    from filings made before a detected split are divided by the split ratio.
    """
    co = _company(ticker, "latest", use_filing_pages)
    out = _base(co, ticker)
    annual = [e for e in co.cal if e["annual"]][-years:]
    rows = []
    for ent in annual:
        f = co.fy_fact(PER_SHARE_SPECS["eps_diluted"], ent, "per_share", adjust=False)
        row = {"ticker": ticker, "fiscal_year": ent["fy"], "fiscal_year_end": _iso(ent["end"]), "form": ent["form"],
               "filed": _iso(ent["filed"]), "eps_diluted": None, "eps_diluted_as_reported_latest": None,
               "eps_diluted_first_reported": None, "split_factor_applied": None, "tag": None,
               "value_from_filing": None, "period_days": None, "net_income": None,
               "weighted_diluted_shares": None, "implied_eps_check": None, "currency": co.currency,
               "source": _src(co.facts_url), "retrieved_at": co.facts_ts}
        if f is not None:
            fac = co.split_factor(f["filed"])
            tax, tag = f["tag"].split(":")
            allv = sorted((x for x in co.facts(tax, tag, co.unit_for("per_share"))
                           if x["start"] == f["start"] and x["end"] == f["end"]), key=lambda x: x["filed"])
            row.update({
                "eps_diluted": round(f["val"] / fac, 6), "eps_diluted_as_reported_latest": f["val"],
                "eps_diluted_first_reported": allv[0]["val"] if allv else None, "split_factor_applied": fac,
                "tag": f["tag"], "period_days": f["days"],
                "value_from_filing": {"form": f["form"], "filed": _iso(f["filed"]), "accession": f["accn"]},
            })
            if not 350 <= f["days"] <= 380:
                row["note"] = f"fiscal period of {f['days']} days (transition period), not a full year"
        ni = co.fy_fact(FLOW_SPECS["net_income"], ent)
        ws = co.fy_fact(SHARE_FLOW_SPECS["weighted_diluted_shares"], ent, "shares", adjust=True)
        if ni is not None:
            row["net_income"] = ni["val"]
        if ws is not None:
            row["weighted_diluted_shares"] = ws["val"]
        if ni is not None and ws is not None and ws["val"]:
            row["implied_eps_check"] = round(ni["val"] / ws["val"], 4)
        rows.append(row)
    cagr = None
    cagr_note = None
    vals = [r["eps_diluted"] for r in rows]
    if len(vals) >= 4 and vals[-1] is not None and vals[-4] is not None:
        if vals[-1] > 0 and vals[-4] > 0:
            cagr = (vals[-1] / vals[-4]) ** (1 / 3) - 1
        else:
            cagr_note = "not meaningful: EPS <= 0 at one end of the 3-year span"
    else:
        cagr_note = "fewer than 4 fiscal years of diluted EPS"
    if co.splits:
        split_note = ("splits detected from restated share counts: " + "; ".join(
            f"{s['ratio']:g}-for-1 ({s['kind']}) between filings of {s['last_filing_on_old_basis']} and "
            f"{s['first_filing_on_new_basis']}" for s in co.splits)
            + ". EPS from filings before the new-basis date were divided by the ratio.")
    else:
        split_note = "no split / share-basis restatement detected in the XBRL history; EPS are as reported"
    out.update({
        "years": rows, "eps_cagr_3y": cagr,
        "eps_cagr_3y_definition": "(diluted EPS latest fiscal year / diluted EPS three fiscal years earlier)^(1/3) - 1, "
                                  "both on the current share basis",
        "eps_cagr_3y_note": cagr_note, "split_adjustment_note": split_note, "notes": list(co.notes),
    })
    return out


def altman_inputs(ticker: str, use_filing_pages: bool = True) -> dict:
    """Latest balance-sheet and TTM inputs for an Altman Z-score (market value of equity is not an EDGAR item)."""
    co, recs = _periods(ticker, 8, "latest", use_filing_pages)
    out = _base(co, ticker)
    fields = {"period_end": None, "filed": None, "form": None, "working_capital": None, "current_assets": None,
              "current_liabilities": None, "retained_earnings": None, "ebit_ttm": None, "total_assets": None,
              "total_liabilities": None, "revenue_ttm": None, "stockholders_equity": None,
              "shares_outstanding": None, "ttm_days": None, "ebit_basis": None, "tags": {}, "notes": []}
    if recs:
        r = recs[-1]
        ca, cl = r["current_assets"], r["current_liabilities"]
        fields.update({
            "period_end": r["period_end"], "filed": r["filed"], "form": r["form"],
            "working_capital": ca - cl if ca is not None and cl is not None else None,
            "current_assets": ca, "current_liabilities": cl, "retained_earnings": r["retained_earnings"],
            "ebit_ttm": r["_ttm"]["ebit"], "total_assets": r["total_assets"],
            "total_liabilities": r["total_liabilities"], "revenue_ttm": r["_ttm"]["revenue"],
            "stockholders_equity": r["stockholders_equity"], "shares_outstanding": r["shares_outstanding"],
            "ttm_days": r["_ttm_days"], "ebit_basis": r["tags"].get("ebit"),
            "tags": {k: r["tags"].get(k) for k in ("current_assets", "current_liabilities", "retained_earnings",
                                                   "total_assets", "total_liabilities", "revenue", "ebit")},
        })
        notes = list(co.notes) + _standing_notes(co, [_public(x) for x in _window(recs, 8)])
        if ca is None or cl is None:
            notes.append("unclassified balance sheet (no current assets / current liabilities): working capital "
                         "unavailable -- Altman Z is not applicable in its standard form")
        if r["_ttm"]["ebit"] is None:
            notes.append("EBIT (TTM) unavailable: see ttm_series notes")
        fields["notes"] = notes
    out.update(fields)
    out["currency"] = co.currency
    return out


def coverage(ticker: str, years: int = 8, use_filing_pages: bool = True) -> dict:
    """Compact availability report for one ticker (what EDGAR does and does not provide)."""
    co, recs = _periods(ticker, years, "latest", use_filing_pages)
    win = _window(recs, years)
    out = _base(co, ticker)
    fields = ("revenue", "gross_profit", "operating_income", "ebit", "depreciation_amortization", "ebitda",
              "net_income", "eps_diluted", "interest_expense", "operating_cash_flow", "capex", "free_cash_flow",
              "cash", "short_term_investments", "total_debt", "total_assets", "total_liabilities", "current_assets",
              "current_liabilities", "retained_earnings", "stockholders_equity", "shares_outstanding",
              "weighted_diluted_shares")
    last = win[-1] if win else None
    out.update({
        "n_periods": len(win), "period_types": dict(Counter(r["period_type"] for r in win)),
        "first_period_end": win[0]["period_end"] if win else None,
        "last_period_end": last["period_end"] if last else None, "last_filed": last["filed"] if last else None,
        "non_null": {f: sum(1 for r in win if r[f] is not None) for f in fields},
        "latest_missing": [f for f in fields if last is not None and last[f] is None],
        "n_ttm_ebitda": sum(1 for r in win if r["_ttm"]["ebitda"] is not None),
        "latest_ttm_ebitda": last["_ttm"]["ebitda"] if last else None,
        "latest_ttm_revenue": last["_ttm"]["revenue"] if last else None,
        "debt_methods_disagree": sum(1 for r in win if r["total_debt_methods_agree"] is False),
        "notes": list(co.notes) + _standing_notes(co, [_public(r) for r in win]),
    })
    return out


# --------------------------------------------------------------------------- CLI
def _summary(ticker: str, use_pages: bool) -> dict:
    q = quarterly_fundamentals(ticker, use_filing_pages=use_pages)
    t = ttm_series(ticker, use_filing_pages=use_pages)
    return {
        "ticker": ticker, "cik": cik_for(ticker),
        "company": {k: q[k] for k in ("entity_name", "sic", "sic_description", "fiscal_year_end_mmdd", "currency",
                                      "periodicity", "report_forms", "splits", "error")},
        "coverage": {k: v for k, v in coverage(ticker, use_filing_pages=use_pages).items()
                     if k in ("n_periods", "period_types", "first_period_end", "last_period_end", "last_filed",
                              "non_null", "latest_missing", "n_ttm_ebitda", "debt_methods_disagree")},
        "tags_used": q["tags_used"], "notes": q["notes"], "fiscal_year_sum_checks": q["fiscal_year_sum_checks"],
        "latest_quarters": q["quarters"][-2:], "latest_ttm": t["quarters"][-2:],
        "annual_eps": annual_eps(ticker, use_filing_pages=use_pages),
        "altman_inputs": altman_inputs(ticker, use_filing_pages=use_pages),
        "recent_8k": recent_8k(ticker, limit=8)["filings"],
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    full = "--full" in args
    use_pages = "--no-pages" not in args
    tickers = [a for a in args if not a.startswith("--")]
    if not tickers:
        print("usage: python3 -m screener.sources.sec_edgar TICKER [TICKER ...] [--full] [--no-pages]",
              file=sys.stderr)
        return 2
    out = {}
    for t in tickers:
        try:
            if full:
                out[t] = {"quarterly_fundamentals": quarterly_fundamentals(t, use_filing_pages=use_pages),
                          "ttm_series": ttm_series(t, use_filing_pages=use_pages),
                          "annual_eps": annual_eps(t, use_filing_pages=use_pages),
                          "altman_inputs": altman_inputs(t, use_filing_pages=use_pages),
                          "submissions": submissions(t, limit=60)}
            else:
                out[t] = _summary(t, use_pages)
        except FetchError as e:
            out[t] = {"ticker": t, "error": f"unavailable: {e}"}
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
