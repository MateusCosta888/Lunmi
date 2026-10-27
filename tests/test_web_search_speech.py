"""The web speech exception cannot weaken existing attachment/desktop boundaries."""
from app.voice.eligibility import speakable_reply


def test_web_summary_rejects_attachments_and_nonweb_operations() -> None:
    args = dict(message='Resumo [src-0123456789abcdef].', status='SUCCESS',
                scope='conversation', web_outcome='SUCCESS', web_sources=1)
    assert speakable_reply(**args, attachments=True) == ''
    for category in ['mutation', 'read_only']:
        assert speakable_reply(**args, operations=[{
            'tool': 'filesystem', 'category': category, 'state': 'completed_verified',
            'verified': True,
        }]) == ''


def test_web_only_summary_remains_speakable_without_citation_ids() -> None:
    assert speakable_reply(
        message='Resumo [src-0123456789abcdef].', status='SUCCESS',scope='conversation',
        web_outcome='SUCCESS',web_sources=1, operations=[{
            'tool': 'web.search', 'category': 'read_only', 'state': 'completed_verified',
            'verified': True,
        }],
    ) == 'Resumo .'
