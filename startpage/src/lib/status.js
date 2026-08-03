// Instance status — the FOUR states the status bar must distinguish (§10), so an
// auth reject, a stale mirror, a closed browser and "never seen" never all look
// like one grey instance. A fifth, healthy, value ('ok') is the connected+fresh
// default. Pure so the classification is unit-testable without a clock.
//
//   rejected — reject_reason set (auth / protocol / origin / duplicate), with time
//   never    — never seen (no last_seen_at)
//   closed   — seen before but not currently connected (browser closed)
//   stale    — connected but the mirror is older than STATE_FRESH-ish (half-open)
//   ok       — connected and fresh

// `now` MUST be in the SERVER clock scale (store.serverNow(), derived from
// StateResponse.server_now): `snapshot_at` / `last_seen_at` are server stamps, and
// comparing them to the laptop's Date.now() turns a couple of seconds of clock drift
// into "зеркало устарело" on every instance forever — or hides a real half-open
// socket. The store owns the offset; this function stays pure.
export function instanceStatus(inst, now, staleMs) {
  if (!inst) return { state: "never", label: "никогда не подключался" };
  if (inst.reject_reason) {
    return { state: "rejected", label: `отклонён: ${inst.reject_reason}`, at: inst.reject_at };
  }
  if (inst.last_seen_at == null) {
    return { state: "never", label: "никогда не подключался" };
  }
  if (!inst.connected) {
    return { state: "closed", label: "закрыт", at: inst.last_seen_at };
  }
  if (inst.snapshot_at == null || now - inst.snapshot_at >= staleMs) {
    return { state: "stale", label: "подключён, зеркало устарело", at: inst.snapshot_at };
  }
  return { state: "ok", label: "на связи", at: inst.snapshot_at };
}

// A live pause countdown label (§7): "HH:MM:SS" / "MM:SS" of remaining time. A
// non-positive / null remaining reads as "00:00" — the row flips to "active" then.
export function formatCountdown(ms) {
  if (ms == null || ms <= 0) return "00:00";
  const total = Math.floor(ms / 1000);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  const pad = (n) => String(n).padStart(2, "0");
  return (h > 0 ? pad(h) + ":" : "") + pad(m) + ":" + pad(s);
}

// The `resume_pending` latch is armed by TWO DIFFERENT events (src/curator/runner.py,
// step 1) and the status bar used to label both of them "Пауза истекла":
//
//   1. a pause the human took EXPIRED by timeout — the first pass after it must not
//      drain the night's backlog unasked, so it computes a plan and waits for a click;
//   2. a CONTINUITY BREAK — `is_continuity_break` found that something under the
//      curator changed since the last pass (the DB's identity or schema version,
//      IDLE_MINUTES, MAIN_INSTANCE_ID, or the restore marker), so it refuses to act on
//      a picture it cannot vouch for and waits for the same click.
//
// Reading (2) as "пауза истекла" tells the owner they stopped something — they did not,
// and there is nothing to "resume". Ground (1) always leaves `paused_until` set (the
// deadline is cleared only by `apply_resume_shift`, i.e. by the confirming pass), while
// ground (2) is reached only when there is no pause at all, so /api/state DOES carry
// enough to tell them apart: the presence of `paused_until`.
//
// What /api/state does NOT carry is WHICH fingerprint component changed — the stored
// and current fingerprints stay in `settings` and never leave the server — so the
// continuity text names the possibilities instead of pretending to know.
// Called only from the not-currently-paused branch of the status bar, so a non-null
// `pausedUntil` here is by construction a deadline in the PAST.
export function resumePendingNotice(pausedUntil) {
  if (pausedUntil != null) {
    return {
      kind: "pause-expired",
      title: "Пауза истекла",
      sub: "ожидание подтверждения",
      explain:
        "Пауза кончилась, но проход не запускается сам: за это время накопилась " +
        "очередь, и первый проход после паузы делается только по кнопке. Ниже — что " +
        "он сделает; кнопка запускает его и возвращает обычную автоматику.",
    };
  }
  return {
    kind: "continuity-break",
    title: "Состояние сервиса изменилось",
    sub: "первый проход посчитан вхолостую",
    explain:
      "С прошлого прохода изменилось окружение курьера — так бывает после обновления " +
      "сервиса, смены настроек (порог бездействия, главный браузер) или восстановления " +
      "базы из резервной копии. Поэтому проход ничего не тронул, а только посчитал план. " +
      "Ниже — что он сделает; кнопка подтверждает план, выполняет его и возвращает " +
      "обычную автоматику. Паузу при этом никто не ставил.",
  };
}

// "кэш от <время>" and similar relative labels use a plain local time string.
export function formatTime(ts) {
  if (ts == null) return "—";
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return "—";
  }
}
