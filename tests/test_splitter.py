import pytest
from models.domain import TelegramMessage
from telegram.splitter import split_message, split_messages, units, OversizedSection


def test_section_splitting_and_continuity():
    sections = [f'Section {i}\n\n' + ('完整 문단. '*12) for i in range(8)]
    message = TelegramMessage('top','TOP 1 — 프로젝트',sections,1)
    next_top = TelegramMessage('top','TOP 2',['short'],2)
    parts = split_messages([message,next_top], 350)
    assert parts[-1].rank == 2
    assert all(part.rank == 1 for part in parts[:-1])
    assert all(units(part.text) <= 350 for part in parts)
    for section in sections:
        assert any(section in part.text for part in parts)


def test_paragraph_boundaries_and_oversized_atom():
    paragraphs = ['😀'*40, '나'*80, '다'*80]
    parts = split_message(TelegramMessage('top','TOP 1',['Title\n\n'+'\n\n'.join(paragraphs)],1),160)
    for paragraph in paragraphs:
        assert any(paragraph in part.text for part in parts)
    with pytest.raises(OversizedSection):
        split_message(TelegramMessage('top','TOP 1',['a'*500],1),160)
