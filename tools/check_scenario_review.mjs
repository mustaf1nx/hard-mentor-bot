/* Проверяет владельческий отчёт на пропуски и смешение ключевых ролей. */
import assert from 'node:assert/strict';
import fs from 'node:fs';

const html = fs.readFileSync('ui-review.html', 'utf8');
const section = name => {
  const start = html.indexOf(`<section id="${name}">`);
  assert(start >= 0, `Нет раздела ${name}`);
  const end = html.indexOf('</section>', start);
  assert(end > start, `Раздел ${name} не закрыт`);
  return html.slice(start, end);
};
for (const name of ['ticket', 'feedback', 'full_cycle', 'materials_photo', 'one_on_one_second']) {
  const part = section(name);
  assert(!part.includes('Действие не найдено'), `${name}: потерянное действие`);
  assert(part.includes('Новый локальный прогон: завершён успешно'), `${name}: нет успешного свежего прогона`);
}
const ticket = section('ticket');
assert(ticket.includes('· Студент</div>'), 'Нет отдельного студента');
assert(!ticket.includes('Студент + Администратор') && !ticket.includes('Администратор + Студент'), 'Студент совмещён с администратором');
assert(!ticket.includes('Что дальше: Следующий шаг: посмотрите участников'), 'Подсказка ведущего попала в сценарий студента');
const cycle = section('full_cycle');
for (const word of ['· Студент</div>', '· Ведущий</div>', '· Автор</div>', '· Администратор</div>', 'Тестовая перемотка времени', 'Цикл закрыт']) {
  assert(cycle.includes(word), `Полный цикл: отсутствует ${word}`);
}
const photo = section('materials_photo');
for (const word of ['Доступных документов: 0', 'Проверка Лектором зафиксирована', 'Симулятор зафиксировал отправку фото']) {
  assert(photo.includes(word), `Фото: отсутствует ${word}`);
}
assert(photo.indexOf('Доступных документов: 0') < photo.indexOf('Проверка Лектором зафиксирована'), 'Нет порядка проверки фото');
assert(photo.indexOf('Проверка Лектором зафиксирована') < photo.lastIndexOf('Симулятор зафиксировал отправку фото'), 'Не показана выдача фото после проверки');
assert(section('one_on_one_second').includes('место занято'), 'Не показан отказ второму студенту');
console.log('Отчёт: роли, последовательность, фото, отказ 1-на-1 и источники ключевых сценариев проверены.');
