import re
from config.rules import RELEVANCE_TERMS, EXCLUDED_DESCRIPTION_TERMS
from models.domain import Repository


def repository_model(data):
    topics = data.get('topics') or []
    description = data.get('description') or ''
    words = (' '.join(topics) + ' ' + description + ' ' + data['full_name']).lower()
    relevant = any(re.search(r'(?<![a-z])' + re.escape(term).replace(r'\-', '[- ]') + r'(?![a-z])', words)
                   for term in RELEVANCE_TERMS)
    excluded = 'fork/mirror' if data.get('fork') or data.get('mirror_url') else ''
    if any(term in words for term in EXCLUDED_DESCRIPTION_TERMS):
        excluded = 'learning sample / assignment / meme / mirror'
    category = 'AI' if any(term in words for term in ('llm', 'machine-learning', 'ai-agent', 'deep-learning')) else 'Developer Tools'
    return Repository(data.get('id'), data['full_name'], data['html_url'], description,
        category, data.get('language'), data.get('created_at'), data.get('pushed_at'),
        data.get('stargazers_count'), data.get('forks_count'), topics, relevant, excluded)
