import pytest
from config.settings import Settings
from config import rules


def test_env_loading_and_no_secrets_in_repr(tmp_path):
    path = tmp_path / '.env'
    path.write_text('GITHUB_TOKEN=private-test-value\nDATABASE_PATH=test.db\n')
    settings = Settings.load(path, {'DATABASE_PATH': 'override.db'})
    assert settings.github_token == 'private-test-value'
    assert 'private-test-value' not in repr(settings)
    assert settings.database_path == 'override.db'
    assert settings.tz.utcoffset(None).total_seconds() == 9 * 3600
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        settings.validate_live()


def test_thresholds():
    assert rules.star_threshold(100) == (50, 25)
    assert rules.star_threshold(50000) == (1500, 2)
    assert rules.star_threshold(99) == (None, None)
