"""Deterministic English Treatment summaries; raw source evidence stays untouched."""
import re
import unicodedata

MAX_TREATMENT_LENGTH = 120
TREATMENT_NORMALIZATION_VERSION = 2
KEYS = {
    '处理方法': 'treatment', '生长时期': 'stage', '发育阶段': 'stage',
    '取样时间': 'sampling', '培养方法': 'medium', '培养温度': 'temp',
    'treatment': 'treatment', 'treatment description': 'treatment',
    'experimental treatment': 'treatment', 'growth condition': 'treatment',
    'growth conditions': 'treatment', 'age': 'age', 'stage': 'stage',
    'dev stage': 'stage', 'developmental stage': 'stage', 'growth stage': 'stage',
    'time': 'sampling', 'timepoint': 'sampling', 'time point': 'sampling',
    'sampling time': 'sampling', 'collection time': 'sampling', 'sampling': 'sampling',
    'medium': 'medium', 'temp': 'temp',
}
MISSING = {'', 'unknown', 'missing', 'not provided', 'not collected', 'not available',
           'na', 'n/a', 'null'}
# Deliberately exact phrases: translating substrings could erase negation or qualifiers.
TRANSLATIONS = {'对照': 'control', '未处理': 'untreated', '无处理': 'untreated',
                '高温胁迫': 'heat stress', '低温胁迫': 'cold stress',
                '盐胁迫': 'salt stress', '干旱胁迫': 'drought stress',
                '幼苗': 'seedling', '开花期': 'flowering', '黑暗': 'darkness',
                '连续黑暗': 'continuous darkness', '红光': 'red light'}
NUMBERS = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
           'six': '6', 'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10',
           'fifteen': '15', 'twenty-six': '26'}


def _compact(value):
    value = unicodedata.normalize('NFKC', value).replace('−', '-').replace('–', '-')
    value = value.replace('μmol', 'umol').replace('µmol', 'umol').replace('°C', ' C')
    value = TRANSLATIONS.get(value, value)
    value = re.sub(r'(\d)\s*(天|小时|分钟|周|个月)(?![\u3400-\u9fff])',
                   lambda m: m[1] + {'天': 'd', '小时': 'h', '分钟': 'min', '周': 'wk', '个月': 'mo'}[m[2]], value)
    value = re.sub(r'\b('+'|'.join(NUMBERS)+r')(?=[ -]+(?:days?|weeks?|months?|hours?))',
                   lambda m: NUMBERS[m[0].lower()], value, flags=re.I)
    for unit, short in [('day', 'd'), ('week', 'wk'), ('month', 'mo'), ('hour', 'h'), ('hr', 'h')]:
        value = re.sub(r'(\d)\s*-?\s*'+unit+r'(?:\(s\)|s)?\b(?:-old)?', r'\1'+short, value, flags=re.I)
        # A literal "(s)" does not end at a word boundary.
        value = re.sub(r'(\d'+short+r')\(s\)', r'\1', value)
    value = re.sub(r'(\d)\s*-\s*d-old\b', r'\1d', value, flags=re.I)
    value = re.sub(r'(\d)\s+(d|h|wk|mo)\b', r'\1\2', value)
    value = re.sub(r'\bMurashige and Skoog\s*\(MS\)', 'MS', value, flags=re.I)
    value = re.sub(r'\band temperature\s*\(([^)]+)\)', r'at \1', value, flags=re.I)
    value = re.sub(r'\bsampled every\b', 'every', value, flags=re.I)
    value = re.sub(r'\band pooled\b', 'pooled', value, flags=re.I)
    value = re.sub(r'\bcontinuous darkness for\b', 'continuous darkness', value, flags=re.I)
    value = re.sub(r'\b(Tip|Base) of (\d+) Stage leaf',
                   lambda m: m[2]+'-stage leaf '+m[1].lower(), value, flags=re.I)
    value = re.sub(r' - L\s*\(([^)]+)\)', r' (\1)', value)
    value = re.sub(r'\blightr\b', 'light', value)
    return ' '.join(value.split()).strip(' ;')


def summarize_treatment(text):
    """Return (summary, review_reason). Never truncate, guess units, or strip untranslated text."""
    text = ' '.join(str(text or '').split())
    text = unicodedata.normalize('NFKC', text)
    if text.casefold() in MISSING:
        return 'unknown', None
    raw_parts = list(dict.fromkeys(p.strip() for p in re.split('[;；]', text) if p.strip()))
    # Some historical tissue records repeat all individual clauses as one extra clause.
    parts = [p for p in raw_parts if len(raw_parts) < 3 or
             p != ' '.join(x for x in raw_parts if x != p)]
    result = []
    for part in parts:
        key, sep, value = part.partition(':')
        category = KEYS.get(key.strip().casefold().replace('_', ' ')) if sep else None
        value = value.strip() if category else part
        if value.casefold() in MISSING:
            continue
        value = _compact(value)
        if category in {'age', 'sampling', 'temp'} or category == 'stage' and re.search(r'\d', value):
            value = category + ': ' + value
        if value and value not in result:
            result.append(value)
    summary = '; '.join(result) or 'unknown'
    if not summary.isascii():
        return 'unknown', 'treatment_requires_translation'
    if len(summary) > MAX_TREATMENT_LENGTH:
        return 'unknown', 'treatment_requires_summary'
    return summary, None


def treatment_issues(value):
    """Catalog boundary check, including candidates replayed from old discovery caches."""
    issues = []
    if not value.isascii(): issues.append('Treatment must contain English ASCII text only')
    if len(value) > MAX_TREATMENT_LENGTH: issues.append('Treatment exceeds 120 characters')
    return issues
