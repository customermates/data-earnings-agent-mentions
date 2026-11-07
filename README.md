# Earnings Season: "agent" mentions in SaaS and CRM filings

How often do listed SaaS and CRM companies say "agent" in what they file with the SEC? Counts per 1,000 words in
10-Q and 10-K reports and in the EX-99.1 earnings releases attached to Item 2.02 8-Ks, with an estimated market cap.
Updated daily by a script from SEC EDGAR; maintained by [Customermates](https://customermates.com).

- `filings.csv`: one row per document, with its SEC source URL.
- `seasons.csv`: one row per company and calendar quarter of the fiscal period end.

## Method

- **agent mentions:** words starting with "agent" (agent, agents, agentic, Agentforce, Agentspace, ...), minus legal
  and financial uses such as "transfer agent", "administrative agent", "agent for service" or "employees and agents".
  `raw_agent_mentions` keeps the unfiltered count.
- **AI mentions:** the token "AI" plus "artificial intelligence".
- **words:** alphabetic tokens of the visible document text (inline XBRL header removed).
- **market cap (estimate):** cover-page shares outstanding (dei:EntityCommonStockSharesOutstanding, all classes) times
  the weekly close of the filing week (Alpha Vantage). Releases use the latest share count filed before them.
- **season:** calendar quarter in which the fiscal period ends (Salesforce's quarter ending 31 Oct counts as Q4).
  For a release filed before its 10-Q, the period is the quarter that ended just before the release.
- **companies:** 25 US filers; see `companies.csv`. Foreign private issuers (20-F/6-K) are not included.

Source: U.S. Securities and Exchange Commission, EDGAR (public domain). Prices: Alpha Vantage. Licence of this
dataset: CC BY 4.0, credit "Customermates Data".
