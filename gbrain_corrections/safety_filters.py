# Exact backfill filters, staged without importing its protected ledger.
import re
SECRET=re.compile(r'(?i)(?:\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[baprs]-[A-Za-z0-9-]{12,}|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+|-----BEGIN .*PRIVATE KEY|\b(?:password|passwd|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|authorization|cvv|cvc|pin)\s*[=:]\s*\S+|\b\d{6,12}:[A-Za-z0-9_-]{25,}|(?:\d[ -]?){13,19})')
SENSITIVE=re.compile(r'(?i)\b(financ\w*|legal|contract|royalt\w*|bank\w*|mortgage|tax\w*|medical|health|personal|immigration|ircc|insurance)\b')
FAMILY=re.compile(r'(?i)\b(family|britt|cosette|clara|holly|eden|bennett|maren|mertle)\b')
EXCLUDED=re.compile(r'(?i)(?:credential|password|secret|token|passport|statement[-_ ]?scan|document[-_ ]?scan|receipt[-_ ]?scan|\.env(?:$|/))')
REJECT=re.compile(r'(?i)(?:password|passwd|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|bearer\s|cvv|cvc|credit card|debit card|account number|routing number|iban|swift code|passport|social security|driver.?s licen[cs]e|document scan|ocr text|data:image/|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\b\d{3}[- .]\d{3}[- .]\d{4}\b|\b\d{3}-\d{2}-\d{4}\b|[A-Za-z0-9+/=_-]{48,})')
CREDENTIAL_CONTEXT=re.compile(r'(?i)(?:\b(?:password|passwd|passphrase|credential|secret|token|api.?key|cvv|cvc|iban|routing.number|account.number)\b|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bAIza[A-Za-z0-9_-]{30,}|\b(?:recovery|seed) phrase\b|\b(?:document|passport|licen[cs]e).{0,12}(?:scan|ocr)\b)')
