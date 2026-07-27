// Probe for the cross-instance window focus spike (§14.1).
// Polls a local control endpoint. The treatment — chrome.windows.update
// ({focused:true}) — is issued EXACTLY ONCE; the server also hands out the
// "focus" command only once, so one-shot discipline is enforced on both sides.
const CTRL = 'http://127.0.0.1:8777';
let focusFired = false;

async function report(obj) {
  try {
    await fetch(CTRL + '/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(obj)
    });
  } catch (e) { /* control server down: nothing to do */ }
}

async function poll() {
  let cmd = '';
  try {
    const r = await fetch(CTRL + '/cmd');
    cmd = (await r.text()).trim();
  } catch (e) { return; }

  const wins = await chrome.windows.getAll({});
  if (!wins.length) return;
  const target = wins[0];

  if (cmd === 'fullscreen') {
    // Setup, not treatment: native fullscreen puts the window on its own Space.
    // Re-read the state afterwards: the request is not always honoured, and a
    // silent no-op would make the arm test a different scenario than intended.
    let err = null;
    try {
      await chrome.windows.update(target.id, { state: 'fullscreen' });
    } catch (e) { err = String(e); }
    const after = await chrome.windows.get(target.id);
    await report({ event: 'fullscreen_setup', windowId: target.id,
                   requested: 'fullscreen', actual: after.state, err });
  } else if (cmd === 'minimize') {
    // Setup, not treatment: puts the window into the state under test.
    await chrome.windows.update(target.id, { state: 'minimized' });
    await report({ event: 'minimized', windowId: target.id });
  } else if (cmd === 'focus' && !focusFired) {
    focusFired = true;
    let err = null;
    try {
      await chrome.windows.update(target.id, { focused: true });
    } catch (e) { err = String(e); }
    await report({ event: 'focus_called', windowId: target.id, state: target.state, err });
  } else if (cmd === 'noop') {
    await report({ event: 'noop_arm' });
  } else if (cmd === 'hello') {
    await report({ event: 'alive', windows: wins.length, state: target.state });
  }
}

setInterval(poll, 1000);
poll();
