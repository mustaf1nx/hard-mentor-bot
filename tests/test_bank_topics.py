"""Regression coverage for adding a topic directly from the material bank."""
import pytest
from aiogram.types import Document
from sqlalchemy import select

from bot.db import LearningMaterial, Subject, Topic
from tests.conftest import ALICE, COORD, LEAD, MENTOR


async def seed_subject(h, with_topic=False):
    async with h.sm() as s:
        subject = Subject(name='Calculus', name_key='calculus')
        s.add(subject)
        await s.flush()
        if with_topic:
            s.add(Topic(subject_id=subject.id, name='Integrals', name_key='integrals'))
        await s.commit()
        return subject.id


@pytest.mark.parametrize('uid', [MENTOR, COORD, LEAD])
@pytest.mark.parametrize('with_topic', [False, True])
async def test_bank_new_topic_continues_material_upload(h, uid, with_topic):
    await h.say(COORD, f'/addmentor {MENTOR}')
    subject_id = await seed_subject(h, with_topic)
    await h.tap(uid, f'ms:topics:bank:{subject_id}:0')
    button = f'ms:newtopic:{subject_id}'
    assert button in h.buttons(h.last_to(uid))
    await h.tap(uid, button)
    assert 'Введите название новой темы' in h.texts_to(uid)[-1]
    await h.say(uid, 'Final Exam Preparation')
    assert 'ms:kind:material' in h.buttons(h.last_to(uid))
    await h.tap(uid, 'ms:kind:material')
    await h.tap(uid, 'ms:visibility:staff')
    await h.say(uid, document=Document(file_id='exam', file_unique_id='exam-unique',
                                      file_name='exam.pdf', file_size=1024,
                                      mime_type='application/pdf'))
    async with h.sm() as s:
        topic = (await s.scalars(select(Topic).where(Topic.name == 'Final Exam Preparation'))).one()
        material = (await s.scalars(select(LearningMaterial))).one()
        assert topic.subject_id == subject_id
        assert material.topic_id == topic.id
        assert material.session_id is None


async def test_new_topic_is_staff_only_and_not_in_schedule(h):
    subject_id = await seed_subject(h)
    button = f'ms:newtopic:{subject_id}'
    await h.tap(ALICE, f'ms:topics:bank:{subject_id}:0')
    assert button not in h.buttons(h.last_to(ALICE))
    await h.tap(ALICE, button)
    await h.say(ALICE, 'Unauthorized topic')
    await h.tap(COORD, f'ms:topics:schedule:{subject_id}:0')
    assert button not in h.buttons(h.last_to(COORD))
    async with h.sm() as s:
        assert not list(await s.scalars(select(Topic)))


async def test_new_topic_cancel_and_missing_subject(h):
    subject_id = await seed_subject(h)
    await h.tap(COORD, f'ms:newtopic:{subject_id}')
    await h.say(COORD, '/cancel')
    await h.say(COORD, 'Cancelled topic')
    await h.tap(COORD, 'ms:newtopic:999999')
    assert 'Предмет не найден' in h.texts_to(COORD)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(Topic)))


async def test_new_topic_replaces_previous_draft(h):
    subject_id = await seed_subject(h)
    await h.say(COORD, '/newsession')
    await h.say(COORD, 'Another subject')
    await h.tap(COORD, f'ms:newtopic:{subject_id}')
    await h.say(COORD, 'Final Exam Preparation')
    assert 'ms:kind:material' in h.buttons(h.last_to(COORD))
    async with h.sm() as s:
        topic = (await s.scalars(select(Topic))).one()
        assert topic.subject_id == subject_id
