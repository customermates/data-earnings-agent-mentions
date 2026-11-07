"""Counts "agent" and "AI" mentions in SEC filing text. Deterministic, no model.

agent_mentions: every word starting with "agent" (agent, agents, agentic, Agentforce, Agentspace, ...), minus
legal and financial uses that have nothing to do with AI agents (transfer agent, administrative agent, paying agent,
"officers, directors, employees and agents", "agent for service", ...). raw_agent_mentions keeps the unfiltered count.
ai_mentions: the standalone token "AI" (case-sensitive, also "AI-powered") plus "artificial intelligence".
words: alphabetic tokens of the visible text; rates are per 1,000 words.
"""
import html
import re

AGENT = re.compile(r'\bagent[a-z]*\b', re.I)
LEGAL = [re.compile(p, re.I) for p in (
    r'\b(?:transfer|paying|administrative|collateral|escrow|exchange|settlement|fiscal|servicing|calculation|depositary|'
    r'registered|syndication|documentation|disbursement|information|solicitation|purchasing|insurance|forwarding|'
    r'placement|sales|selling|subscription|tabulation|rights|warrant|conversion|issuing|auction|custodial|process|'
    r'paying\s+and\s+transfer|registrar\s+and\s+transfer|co-?documentation|co-?syndication|lead\s+arranger\s+and|'
    r'successor|replacement|managing|book-?running|joint|travel|real\s+estate|leasing|shipping|withholding|tax|'
    r'trustee\s+and|registrar\s+and|bank\s+as)\s+agents?\b',
    r'\bagents?\s+for\s+service\b',
    r'\b(?:as|its|the)\s+(?:sole\s+)?agents?\s+(?:for|of|under|on\s+behalf)\b',
    r'\b(?:employees|officers|directors|representatives|affiliates|subsidiaries|attorneys|advisors|contractors|'
    r'partners|managers|members|trustees|fiduciaries|consultants|vendors|resellers|distributors|licensees|'
    r'successors|assigns|nominees|custodians)\s*,?\s*(?:and|or|and/or)\s+agents?\b',
    r'\bagents?\s*,?\s*(?:and|or)\s+(?:employees|representatives|attorneys|advisors|contractors|affiliates|'
    r'subsidiaries|assigns|successors|officers|directors)\b',
    r'\bagents?\s+of\s+(?:the\s+)?(?:company|registrant|corporation|issuer|lenders|holders|purchasers|underwriters)\b',
    r'\b(?:chemical|biological|therapeutic|contrast|imaging|infectious|antimicrobial|antiviral|blowing|cleaning|'
    r'reducing|oxidizing|bonding|curing|foaming)\s+agents?\b',
    r'\bagent\s+bank\b', r'\bagency\s+agreement\b',
)]
AI = re.compile(r'(?<![A-Za-z])AI(?![a-z])')
ARTIFICIAL = re.compile(r'\bartificial\s+intelligence\b', re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
TAG = re.compile(r'<[^>]+>')
DROP = re.compile(r'<(script|style|head)[^>]*>.*?</\1>|<ix:header>.*?</ix:header>|<!--.*?-->', re.I | re.S)


def visible_text(raw):
    """HTML or plain filing text -> visible text (inline XBRL hidden header dropped)."""
    text = DROP.sub(' ', raw)
    text = re.sub(r'<(br|p|div|tr|li|h\d)[^>]*>', '\n', text, flags=re.I)
    text = html.unescape(TAG.sub(' ', text))
    return re.sub(r'[ \t\r\f\v\xa0]+', ' ', text)


def legal_spans(text):
    spans = []
    for pattern in LEGAL:
        spans += [m.span() for m in pattern.finditer(text)]
    return spans


def count(text):
    raw = [m.span() for m in AGENT.finditer(text)]
    spans = legal_spans(text)
    agent = [s for s in raw if not any(a <= s[0] < b for a, b in spans)]
    words = len(WORD.findall(text))
    ai = len(AI.findall(text)) + len(ARTIFICIAL.findall(text))
    return {
        'words': words,
        'agent_mentions': len(agent),
        'raw_agent_mentions': len(raw),
        'ai_mentions': ai,
        'agent_per_1k': round(1000 * len(agent) / words, 3) if words else 0.0,
        'ai_per_1k': round(1000 * ai / words, 3) if words else 0.0,
    }
