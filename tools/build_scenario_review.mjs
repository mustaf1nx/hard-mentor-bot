/* Отчёт из записей симулятора. Никакие данные production не читаются. */
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const dir = path.join(root, 'tools', 'scenarios');
const read = p => JSON.parse(fs.readFileSync(p, 'utf8').replace(/^\uFEFF/, ''));
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const clear = value => String(value ?? '').replace(/<\/?(?:b|i|u|s|code|pre|tg-spoiler|blockquote)>/gi, '');
const nl = value => esc(clear(value)).replaceAll('\n', '<br>');

const info = {
  one_on_one: ['Создание и запись на 1-на-1', 'Администратор создаёт занятие; отдельный студент записывается.', 'Последовательность длинная; дата, место и подтверждение видны.'],
  one_on_one_second: ['Занятое место 1-на-1', 'Два разных студента открывают одну консультацию.', 'Важно видеть, что второе место недоступно, до заполнения билета.'],
  student_cancel: ['Запись и отмена', 'Студент записывается и отказывается от записи.', 'Карточка после записи содержит отмену и необязательный чек-лист.'],
  ticket: ['Входной билет и чек-лист', 'Отдельный обычный студент вводит вопрос и свои попытки.', 'Подсказки ошибок полезны, но три шага занимают время.'],
  feedback: ['Письменный отзыв', 'Студент оценивает занятие и вводит обязательный текст.', 'Нейтральный вопрос понятен; пять шагов могут утомлять.'],
  full_cycle: ['Полный цикл одного занятия', 'Запись → посещаемость → проведение → итоговый материал → фиксация проверки → отзыв → карточка администрации.', 'Занятие создаётся заранее; после записи стенд искусственно сдвигает его время. Роли разделены. Автор находит назначенное занятие в разделе «Я веду / готовлю».'],
  mentor_manage: ['Ведущий: управление занятием', 'Ментор открывает занятие и участников.', 'Подсказка «Что дальше» помогает начать работу.'],
  author_flow: ['Назначение Автора', 'Назначается Автор занятия.', 'Смена ролей и результата должна быть понятна из ответов.'],
  materials: ['Материалы и проверка', 'Загрузка документа и доступ студенту после проверки.', 'Статус проверки до публикации требует понятных формулировок.'],
  materials_photo: ['Фотография заметок', 'Автор загружает фото; студент ищет его до проверки и открывает после.', 'Проверяются два состояния на отдельных аккаунтах.'],
  dash: ['Администрация: обзор', 'Недельная сводка, цикл, долги и карточка.', 'Сводка содержит много кнопок; итогам и проблемам нужна ясная иерархия.'],
  dashboard_filters: ['Фильтры и возврат', 'Администратор выбирает период и предмет, открывает занятие и возвращается.', 'Контекст фильтров должен оставаться видимым.'],
  format_past: ['Формат прошедшего занятия', 'Администратор выбирает и подтверждает неизвестный формат.', 'Предпросмотр важен, поскольку от формата зависят показатели.'],
  staff_directory: ['Люди и назначения', 'Администратор открывает людей, зоны и профиль.', 'Настоящие зоны пока не утверждены; здесь только демонстрационные данные.'],
  report: ['Еженедельный отчёт', 'Ментор отвечает на вопросы об итогах недели.', 'Числа считаются ботом; свободный текст может требовать времени.'],
  final: ['Итоговый отчёт', 'Ментор описывает итоги и сложности триместра.', 'Подсказки помогают понять, какую информацию вводить.'],
  support_ticket: ['Обращение и ответ', 'Студент пишет обращение; администрация отвечает.', 'Обращения и отзывы о занятии — разные функции.'],
  roles: ['Роли по предметам', 'Администратор просматривает и задаёт роли.', 'Здесь роль по предмету, а не зона ответственности стаффа.'],
  bulk: ['Большие списки', 'Просмотр сводки на демонстрационных данных.', 'Функциональная проверка страниц; не нагрузочный тест.'],
  autocancel: ['Автоотмена', 'Тестовая задача проверяет отмену по правилу спроса.', 'Часть результата находится в базе и не является диалогом.'],
  _audit_student: ['Права обычного студента', 'Студенту отказывают в административной команде.', 'Отдельный аккаунт студента без прав администрации.']
};
const titles = Object.keys(info);
const roles = {admin:'Администратор', student:'Студент', lead:'Ведущий', mentor:'Ментор', author:'Автор', first:'Студент 1', second:'Студент 2'};
const actionKeys = ['command','say','tap','tap_label','tap_first','tap_prefix','document','photo'];

function inputLabel(step, input) {
  if ('command' in step) return `Вводит команду ${step.command}`;
  if ('say' in step) return `Пишет: «${input?.text ?? step.say}»`;
  if ('tap_label' in step) return `Нажимает «${step.tap_label}»`;
  if ('tap_first' in step || 'tap_prefix' in step) {
    const label = String(input?.text || '').replace(/^→\s*/, '');
    if (label && !label.includes('…') && !label.startsWith('кнопка #')) return `Нажимает «${label}»`;
    return 'Выбирает кнопку из предыдущего экрана; её название в записи не сохранилось';
  }
  if ('tap' in step) return 'Открывает экран по тестовой команде; путь по кнопкам в этом шаге не показан';
  if ('photo' in step) return 'Отправляет тестовую фотографию';
  if ('document' in step) return `Отправляет тестовый файл «${step.document}»`;
  return 'Выполняет действие';
}
function buttons(msg) {
  const rows = msg?.markup?.inline_keyboard ?? [];
  return rows.length ? `<div class="keys">${rows.map(r => `<div class="keyrow">${r.map(b => `<span class="key">${esc(b.text)}</span>`).join('')}</div>`).join('')}</div>` : '';
}
function messageHtml(m) {
  const kind = m.kind === 'sendPhoto' ? 'Симулятор зафиксировал отправку фото; само изображение в записи отсутствует' : m.kind === 'sendDocument' ? 'Симулятор зафиксировал отправку документа; содержимое файла в записи отсутствует' : 'Бот показал сообщение';
  return `<div class="bubble"><div class="msgkind">${kind}</div><div>${nl(m.text || '(содержимое сообщения не записано)')}</div>${buttons(m)}</div>`;
}
function isBackground(m, step) {
  const t = clear(m.text);
  const expected = String(step.expect || '').toLowerCase();
  if (expected && t.toLowerCase().includes(expected)) return false;
  return /Как прошла менторка #|SLA просрочен/i.test(t);
}
function messageMatches(m, step) {
  const text = clear(m.text).toLowerCase();
  if (step.expect && text.includes(String(step.expect).toLowerCase())) return true;
  if (step.expect_kind && m.kind === step.expect_kind) return true;
  const buttons = (m.markup?.inline_keyboard ?? []).flat().map(x => String(x.text).toLowerCase());
  return !!step.expect_button && buttons.some(x => x.includes(String(step.expect_button).toLowerCase()));
}

function getScenario(name) {
  const src = path.join(dir, `${name}.json`);
  const clean = path.join(dir, `_clean_${name}.json`);
  const old = path.join(dir, `_run_${name}.json`);
  const transcript = fs.existsSync(clean) ? clean : old;
  if (!fs.existsSync(src) || !fs.existsSync(transcript)) return {name, missing:true};
  const s = read(src), t = read(transcript);
  const log = path.join(dir, `_clean_${name}.log`);
  const rawLog = fs.existsSync(log) ? fs.readFileSync(log) : null;
  const logText = rawLog ? rawLog.toString(rawLog[0] === 0xff && rawLog[1] === 0xfe ? 'utf16le' : 'utf8') : '';
  const failed = /\bFAIL\b|Traceback \(most recent call last\)/.test(logText);
  const passed = logText.includes('переписка сохранена:') && !failed;
  const userMap = {...(s.users || {})};
  if (!('admin' in userMap)) userMap.admin = s.admin || 100500;
  const aliases = new Map();
  for (const [role, id] of Object.entries(userMap)) {
    const key = String(id); if (!aliases.has(key)) aliases.set(key, []); aliases.get(key).push(roles[role] || role);
  }
  const positions = new Map();
  const events = [];
  for (let index=0; index<(s.steps || []).length; index++) {
    const step = s.steps[index];
    if (step.fixture_end_cycle_lesson) { events.push({fixture:true, number:index+1}); continue; }
    if (!actionKeys.some(k => Object.hasOwn(step,k))) continue;
    const id = String(step.user_id || userMap[step.as || 'admin'] || s.admin || 100500);
    const acts = t.inputs?.[id] || [];
    const cursor = positions.get(id) || 0;
    const act = acts[cursor]; positions.set(id, cursor+1);
    if (!act) { events.push({number:index+1, role:roles[step.as] || 'Администратор', missing:true}); continue; }
    const list = t.messages?.[id] || [];
    const from = Number(act.after ?? 0), to = act.until != null ? Number(act.until) : cursor+1 < acts.length ? Number(acts[cursor+1].after ?? list.length) : list.length;
    let out = list.slice(from, Math.max(from,to)).filter(m => m.kind !== 'deleteMessage');
    // Снятие клавиатуры с прошлого сообщения — служебное действие, не ответ.
    const keysRemoved = out.some(m => m.kind === 'editMessageReplyMarkup' && !m.text && !(m.markup?.inline_keyboard ?? []).length);
    out = out.filter(m => !(m.kind === 'editMessageReplyMarkup' && !m.text && !(m.markup?.inline_keyboard ?? []).length));
    const background = out.filter(m => isBackground(m, step));
    out = out.filter(m => !isBackground(m, step));
    const matched = out.filter(m => messageMatches(m, step));
    // С точной границей (until) все сообщения промежутка — ответ на это действие.
    const ambiguous = act.until == null && out.length > 1 && matched.length !== 1;
    // Не прячем дополнительные ответы: бот может прислать подтверждение и затем карточку.
    const primary = out;
    const extras = background;
    const roleList = aliases.get(id) || [roles[step.as] || 'Участник'];
    events.push({number:index+1, role:roleList.join(' + '), action:inputLabel(step,act), primary, extras, ambiguous, keysRemoved, expected:step.expect || step.expect_button || null, approximate:act.until == null});
  }
  const hasMixedStudent = [...aliases.values()].some(a => a.includes('Студент') && a.includes('Администратор'));
  return {name, s, events, passed, failed, clean:transcript===clean, hasMixedStudent};
}
function eventHtml(e) {
  if(e.fixture) return `<div class="fixture"><strong>Тестовая перемотка времени.</strong> Стенд переносит занятие в прошлое, чтобы проверить действия после его окончания. Системные часы не менялись.</div>`;
  if(e.missing) return `<article class="step"><div class="stephead">Шаг ${e.number} · ${esc(e.role)}</div><p class="warning">Действие не найдено в записи; экран не восстановлен.</p></article>`;
  const noOutput = `<div class="uncertain">В записи нет отдельного сообщения бота после этого действия. Результат здесь не подтверждён изображением.</div>`;
  const warning = e.ambiguous ? '<div class="uncertain">Записано несколько сообщений в этом промежутке; их связь с действием не установлена. Показаны все, без догадок.</div>' : e.approximate ? '<div class="uncertain">В старой записи нет точной границы ответа; в промежутке могут быть и другие сообщения.</div>' : '';
  const extras = e.extras?.length ? `<details class="extra"><summary>Другие сообщения в том же промежутке: ${e.extras.length} (могут быть фоновыми)</summary>${e.extras.map(messageHtml).join('')}</details>` : '';
  const removed = e.keysRemoved ? '<div class="msgkind">Кнопки предыдущего сообщения убраны.</div>' : '';
  return `<article class="step"><div class="stephead">Шаг ${e.number} · ${esc(e.role)}</div><div class="action">${esc(e.action)}</div><div class="answers">${warning}${removed}${e.primary?.length ? e.primary.map(messageHtml).join('') : noOutput}</div>${extras}</article>`;
}

const data = titles.map(getScenario);
const nav = data.map(d => `<li><a href="#${esc(d.name)}">${esc(info[d.name][0])}</a><span>${d.missing ? 'нет записи' : d.clean ? (d.passed ? 'новый прогон прошёл' : d.failed ? 'новый прогон не прошёл' : 'результат не подтверждён') : 'предыдущая запись'}</span></li>`).join('');
const sections = data.map(d => {
  const [title, description, opinion] = info[d.name];
  if (d.missing) return `<section id="${esc(d.name)}"><h2>${esc(title)}</h2><p>Запись сценария не найдена; восстановить шаги нельзя.</p></section>`;
  const mixed = d.hasMixedStudent ? '<p class="warning"><strong>Внимание:</strong> в этом старом сценарии студент и администратор — один аккаунт. Не используйте его для оценки прав обычного студента.</p>' : '';
  const provenance = d.clean ? (d.passed ? 'Новый локальный прогон: завершён успешно.' : d.failed ? 'Новый локальный прогон не прошёл; показана частичная запись до ошибки.' : 'Новая запись есть, но успешное завершение по журналу не подтверждено.') : 'Предыдущая запись симулятора; в этом обновлении сценарий заново не запускался.';
  return `<section id="${esc(d.name)}"><h2>${esc(title)}</h2><p>${esc(description)}</p><p><strong>Оценка удобства:</strong> ${esc(opinion)}</p><p class="provenance">${esc(provenance)} Данные тестовые.</p>${mixed}<details class="scenario"><summary>Открыть шаги: ${d.events.length}</summary><p class="reading">Каждая карточка: кто действует → что делает → что зафиксировал бот после этого действия. Технические команды обозначены отдельно. Если сообщение не записано, это сказано прямо.</p>${d.events.map(eventHtml).join('')}</details><p class="sources"><a href="tools/scenarios/${esc(d.name)}.json">Сценарий</a> · <a href="tools/scenarios/${d.clean ? '_clean_' : '_run_'}${esc(d.name)}.json">Исходная запись</a></p></section>`;
}).join('');

const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Сценарии бота: кто что делает</title><style>
:root{--ink:#172c3d;--blue:#174c68;--paper:#f5f8f9;--border:#cad8df;--soft:#e9f1f5}*{box-sizing:border-box}body{margin:0;color:var(--ink);background:var(--paper);font:17px/1.6 system-ui,-apple-system,Segoe UI,sans-serif}header{background:#123b50;color:#fff;padding:40px max(20px,calc((100vw - 940px)/2))}h1{font-size:clamp(2rem,5vw,3.1rem);line-height:1.08;margin:0 0 14px}header p{color:#e0edf2;max-width:70ch;margin:0}main{max-width:980px;padding:0 20px 60px;margin:auto}section{padding:27px 0;border-bottom:1px solid var(--border)}h2{font-size:1.45rem;line-height:1.25;margin:0 0 12px}p{max-width:80ch}a{color:#075b91;text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:3px solid #cc790e;outline-offset:3px}.notice{background:#e4eff3;padding:18px 20px;margin:20px 0}.warning,.uncertain{background:#fff0d7;color:#633f00;padding:12px 14px}.index{columns:2;list-style:none;padding:0}.index li{break-inside:avoid;margin-bottom:9px}.index span{display:block;font-size:.8em;color:#566b77}.scenario{margin-top:18px}.scenario>summary{background:#174c68;color:#fff;padding:12px 15px;cursor:pointer;font-weight:700}.reading,.provenance,.sources{font-size:.9em;color:#4d6573}.step{background:#fff;border:1px solid var(--border);margin:15px 0;padding:17px;max-width:760px}.stephead{font-weight:750;color:#174c68;margin-bottom:8px}.action{background:#dcefea;padding:10px 13px}.answers{margin-top:10px}.bubble{background:#eef3f5;padding:13px;margin-top:8px;white-space:normal;overflow-wrap:anywhere}.msgkind{font-size:.8em;color:#4d6573;font-weight:700;margin-bottom:6px}.keys{margin-top:12px}.keyrow{display:flex;flex-wrap:wrap;gap:5px;margin:5px 0}.key{background:#d4e7ef;padding:6px 9px;min-width:120px;flex:1}.extra{font-size:.9em;margin-top:10px;color:#4d6573}.extra summary{cursor:pointer}.fixture{padding:15px;background:#fcefd5;margin:18px 0}.sources{margin-top:12px}@media(max-width:680px){.index{columns:1}body{font-size:16px}.step{padding:12px}}
</style></head><body><header><h1>Как работает бот: сценарии по шагам</h1><p>Читается сверху вниз: отдельное действие человека, затем записанные сообщения бота. У каждого шага указана роль.</p></header><main><section><h2>Как пользоваться отчётом</h2><div class="notice">Это интерактивная визуализация записей симулятора, а не снимки приложения Telegram. Данные демонстрационные. Фоновые сообщения вынесены отдельно; если установить связь ответа с действием невозможно, это указано. Несколько ролей в одном тестовом аккаунте также явно обозначены.</div><p>Откройте нужный сценарий. Внутри — последовательность всех записанных шагов без обрезанных картинок. Скрипт бота проверялся на фейковом API; в настоящем Telegram поведение и внешний вид ещё нужно подтвердить.</p><p><strong>Полный цикл:</strong> запись студента выполнена в том же прогоне, после чего стенд искусственно сдвинул время окончания занятия. <strong>Фотография:</strong> отдельно показаны банк до проверки и выдача после неё.</p><p><strong>Отдельно:</strong> «Респондент» отвечает в предметном чате. Специального сценария его работы в боте пока нет; отсутствие вопросов не является долгом.</p><ol class="index">${nav}</ol></section>${sections}<footer><p>Отчёт создан из локальных сценариев и журналов. Тестовые примеры не являются реальными именами, датами и аудиториями. <a href="scenario-evidence-review.html">Предыдущий аудит и замечания</a></p></footer></main></body></html>`;
fs.writeFileSync(path.join(root, 'ui-review.html'), html, 'utf8');
if (process.argv.includes('--qa')) {
  const start = html.indexOf('<section id="ticket">');
  const end = html.indexOf('</section>', start);
  const ticketSection = html.slice(start, end + '</section>'.length).replace('<details class="scenario">', '<details class="scenario" open>');
  fs.writeFileSync(path.join(root, 'shots', 'review-open-qa.html'), html.slice(0, html.indexOf('<main>') + 6) + ticketSection + '</main></body></html>', 'utf8');
}
console.log(`Обновлён ui-review.html: ${data.length} сценариев, ${data.filter(x=>x.clean&&x.passed).length} подтверждены новыми записями.`);
for (const d of data) {
  if (d.missing) { console.log(`${d.name}: запись отсутствует`); continue; }
  const empty = d.events.filter(e => !e.fixture && (e.missing || !e.primary?.length)).length;
  const direct = d.events.filter(e => e.action?.includes('тестовой команде')).length;
  const ambiguous = d.events.filter(e => e.ambiguous).length;
  if (empty || direct || ambiguous || !d.passed) console.log(`${d.name}: без ответа=${empty}, технических переходов=${direct}, неоднозначных=${ambiguous}, прошёл=${d.passed}`);
}
