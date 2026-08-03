// What a rule-change preview MEANS, in words (§8).
//
// The server gates a rule change on THREE independent grounds (`_requires_confirm`,
// src/api/rules.py) and only one of them is a number:
//
//   (a) relocations + closures > 0 — tabs would actually move/close;
//   (b) the rule set crosses the empty↔non-empty boundary — creating the FIRST active
//       rule switches the fleet-wide "unruled → main" drain on, deleting the last one
//       switches curation off entirely;
//   (c) some instance could not be counted, so the numbers are an under-report.
//
// Showing only (a) is how the editor came to print "Переселений: 0, закрытий: 0 —
// требуется подтверждение": zeros, an armed confirm button, and not a word about the
// reason — which was (b), by far the largest thing the system ever does.
//
// The payload does not name the ground, so it is derived. `enables_drain` /
// `disables_curation` describe the CANDIDATE rule set only (`enables_drain` is just
// `has_active_rules(candidate)`, true for every non-empty set — NOT "this is the first
// rule"), so (b) is identified by ELIMINATION: if confirmation is required while the
// counts are zero and everyone was counted, nothing else is left. That elimination is
// valid only for create/update — a DELETE is gated unconditionally — which is why `op`
// is an argument here and not guessed. The popup (extension/pages/popup.js) makes the
// same derivation for the only op it has.

const OP_DELETE = "delete";

export function confirmGround(preview, op) {
  const p = preview || {};
  const impact = (p.relocations || 0) + (p.closures || 0);
  const uncounted = notCountedIds(p);
  if (p.disables_curation) return "disables_curation";
  if (op === OP_DELETE) return impact > 0 ? "impact" : "delete";
  if (impact > 0) return "impact";
  if (uncounted.length) return "uncounted";
  if (p.enables_drain) return "first_rule";
  return "unknown";
}

// A 409 body names the uncountable instances in `not_counted`; a plain preview carries
// them as `instances[].counted === false`. Accept both.
export function notCountedIds(preview) {
  const p = preview || {};
  const ids = Array.isArray(p.not_counted)
    ? p.not_counted.map((i) => (i && i.id) || i)
    : (p.instances || []).filter((i) => i && !i.counted).map((i) => i.id);
  return ids.filter((id) => typeof id === "string" && id);
}

// The lines of the impact block: the numbers, what the numbers actually mean, and the
// ground confirmation is being asked on. `requiresConfirm` overrides the payload's own
// flag (a 409 body IS the request for confirmation, whether or not it echoes the field).
export function impactLines(preview, { op = "create", requiresConfirm = null } = {}) {
  const p = preview || {};
  const r = p.relocations || 0;
  const c = p.closures || 0;
  const gated = requiresConfirm === null ? Boolean(p.requires_confirm) : Boolean(requiresConfirm);
  const lines = [
    `Переселений: ${r}, закрытий: ${c} — столько сделает ближайший проход.`,
    // A zero is NOT "the rule matches nothing". §7 step 4 / preview.py `_guarded` skip a
    // tab that has not been idle long enough (and pinned / audible / on-screen ones,
    // always), so a site you were reading a minute ago matches and still counts 0.
    "Это не «сколько вкладок подходит под правило»: вкладку, которой недавно " +
      "пользовались, а также закреплённую, звучащую или открытую на экране, проход " +
      "пока не трогает.",
  ];
  const ground = confirmGround(p, op);
  if (ground === "disables_curation") {
    lines.push(
      "Это последнее активное правило: после сохранения курирование выключится " +
        "полностью — пока не появится новое правило, ничего не переселяется и не " +
        "закрывается нигде.",
    );
  } else if (gated && ground === "first_rule") {
    lines.push(
      "Это будет первое активное правило — поэтому подтверждение нужно даже при нулях: " +
        "с ним включается курирование всего парка, и с ближайшего прохода в главный " +
        "браузер уезжает КАЖДАЯ вкладка без подходящего правила, а не только вкладки " +
        "этого правила.",
    );
  } else if (gated && ground === "delete") {
    lines.push(
      "Удаление подтверждается всегда: вкладки этого правила теряют дом и на ближайшем " +
        "проходе уезжают в главный браузер.",
    );
  }
  const uncounted = notCountedIds(p);
  if (uncounted.length) {
    lines.push(
      "Не посчитаны (нет связи или устаревшее зеркало): " + uncounted.join(", ") +
        ". Их вкладки в числа выше не вошли, поэтому реальный масштаб может быть больше.",
    );
  }
  return lines;
}

// One line for the button/row next to the numbers: WHY confirmation is being asked.
export function confirmHeadline(preview, op = "create") {
  const p = preview || {};
  switch (confirmGround(p, op)) {
    case "disables_curation":
      return "подтвердите: курирование выключится полностью";
    case "impact":
      return `подтвердите: ${p.relocations || 0} переселений и ${p.closures || 0} закрытий на ближайшем проходе`;
    case "uncounted":
      return "подтвердите: часть браузеров не удалось посчитать, реальный масштаб неизвестен";
    case "first_rule":
      return "подтвердите: это первое правило, оно включает переселение «без правила → главный» для всего парка";
    case "delete":
      return "подтвердите удаление правила";
    default:
      return "требуется подтверждение";
  }
}
