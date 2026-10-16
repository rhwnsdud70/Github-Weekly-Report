from scripts.verify_scoring_cold_start import candidates, period
from database.db import Database


def test_fixture_cold_start_scores_and_ranks_without_external_services(tmp_path):
    db = Database(tmp_path / 'fixture.db')
    try:
        items = candidates(db, period())
        from services.ranking_service import select_top
        top, _ = select_top(items, period())
        first = next(item for item in items if item.repository.github_id == 1)
        repeat = next(item for item in items if item.repository.github_id == 10)
        assert len(items) == 10
        assert len(top) == 5 and len({item.repository.identity for item in top}) == 5
        assert first.metric('star_delta').value == 215
        assert first.score.components['release'] == 7
        assert any(item.critical for item in top)
        assert 'previously reported without meaningful new change' in repeat.validation_reasons
        assert all(item.repository.category == 'AI' for item in top)
    finally:
        db.close()
