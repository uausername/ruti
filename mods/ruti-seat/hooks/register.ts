import type { Register } from 'claude-code'

// ruti decides; this mod only carries the decision out. On a fresh prompt it asks
// `ruti seat plan` (which classifies the prompt and applies the policy: up at once,
// down only with good reason, model switches only when cheap) and
//   * sets the model right away -- `$.config.set` is allowed inside the prompt hook,
//     though the prompt already submitted still runs on the old model: the switch
//     shows from the next turn;
//   * keeps the effort for `turn.complete` -- `/effort` is refused while the hook holds
//     the turn, so it takes effect for the turn after.
// In `shadow` mode the plan comes back with apply=false and nothing is touched.
//
// `$.config.set` and `/effort` both save what they set as the default for new
// sessions, in ~/.claude/settings.json, and neither has a session-only form. A seat
// chosen for one session's task must not become every next session's, so around each
// switch the mod puts `model` and `modelSettings` back as they were. The running
// session keeps its new seat: the engine does not re-read the file mid-session.

// One file per session: every session's mod instance rewrites its whole file, so a shared
// one is clobbered by whichever session wrote last and the evidence is gone.
const LOG_DIR = 'C:/Users/Lenovo Gaming/.claude/dev-mods'
let LOG = `${LOG_DIR}/ruti-seat.log`
const WITH_1M = new Set(['sonnet', 'opus', 'fable'])
// `/effort` from a mod raises the "Change effort level?" dialog a typed one does, while
// the cache is warm on a model other than Fable. On Opus 5.5 and Sonnet 5.5 its warning
// is wrong (the cache survives an effort change), so the helper answers it: it reads the
// session's screen and presses `1` only on that dialog, for that level.
const HELPER = 'C:/mycode/ruti/mods/ruti-seat/effort_confirm.py'
const HELPER_TIMEOUT_S = 8
const DIALOG_MODELS = ['opus', 'sonnet']

type Plan = { apply?: boolean; action?: { model?: string; effort?: string }; best?: string; mode?: string }

const lines: string[] = []

// What a switch may write into the user's settings, and so what is put back after it.
const KEPT = ['model', 'modelSettings'] as const
type Kept = Partial<Record<(typeof KEPT)[number], unknown>>

async function userSettingsPath($: any): Promise<string> {
  const dir = await $.env.get('CLAUDE_CONFIG_DIR')
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  return `${dir ?? `${home}/.claude`}/settings.json`
}

// Read from the file, not `$.settings.read`: that answers the engine's in-memory copy,
// which already holds the previous switch's value, so it would be "restored" as the default.
async function snapshotDefaults($: any): Promise<Kept> {
  const user = JSON.parse(await $.fs.read(await userSettingsPath($)))
  const kept: Kept = {}
  for (const key of KEPT) kept[key] = user[key]
  return kept
}

// Puts the kept keys back, touching nothing else in the file. A key that was absent
// before the switch is removed again.
async function restoreDefaults($: any, kept: Kept, why: string): Promise<void> {
  try {
    const path = await userSettingsPath($)
    const text: string = await $.fs.read(path)
    const settings = JSON.parse(text)
    let changed = false
    for (const key of KEPT) {
      if (JSON.stringify(settings[key]) === JSON.stringify(kept[key])) continue
      if (kept[key] === undefined) delete settings[key]
      else settings[key] = kept[key]
      changed = true
    }
    if (!changed) return
    const LF = String.fromCharCode(10)
    const CRLF = String.fromCharCode(13, 10)
    const eol = text.includes(CRLF) ? CRLF : LF
    await $.fs.write(path, JSON.stringify(settings, null, 2).split(LF).join(eol) + eol)
    await note($, `defaults restored after ${why}`)
  } catch (err) {
    await note($, `defaults not restored after ${why}: ${String(err)}`)
  }
}

async function note($: any, line: string): Promise<void> {
  lines.push(`${new Date(await $.clock.now()).toISOString()} ${line}`)
  if (lines.length > 200) lines.shift()
  try { await $.fs.write(LOG, lines.join('\n') + '\n') } catch { /* the log is a convenience */ }
}

// A `/goal` set in a session keeps it working after `ruti flow handoff` has passed the task
// to a new one, so the goal is cleared once the handoff is written. `ruti flow handoff`
// prints this line only for a handoff it accepted; a refused one (no valid title) does not.
const HANDOFF_CALL = /\bruti(\.exe)?\s+flow\s+handoff\b/
const HANDOFF_WRITTEN = 'handoff written:'

// `/goal clear`, or why it did not go through. A command refused mid-turn may come back
// as an error rather than throw, so anything but an empty result is treated as not done.
async function clearGoal($: any, when: string): Promise<boolean> {
  try {
    const result = await $.command.run({ command: 'goal', args: 'clear' })
    const done = !result || Object.keys(result).length === 0
    await note($, `/goal clear (${when}): ${JSON.stringify(result)}`)
    if (done) $.ui.toast('ruti: goal cleared after the flow handoff')
    return done
  } catch (err) {
    await note($, `/goal clear (${when}) failed: ${String(err)}`)
    return false
  }
}

// Once the handoff is written and the next session open, this one closes itself -- unless
// the Stop hook saw background work still running here (a keep-alive, a server), which
// `/exit` would kill. `ruti flow may-close` waits for the Stop hook's launch to land.
async function closeIfHandedOff($: any): Promise<void> {
  try {
    const id = await $.session.id()
    const run = await $.process.run(['ruti', 'flow', 'may-close', '--session', id, '--wait', '10'],
      { timeoutMs: 15000 })
    const verdict = JSON.parse(run.stdout || '{}') as { close?: boolean; why?: string; busy?: string[] }
    await note($, `flow may-close: ${run.stdout.trim()}`)
    if (!verdict.close) {
      if (verdict.busy?.length) $.ui.toast(`ruti flow: window kept open -- still running: ${verdict.busy.join('; ')}`)
      return
    }
    const result = await $.command.run({ command: 'exit', args: '' })
    await note($, `/exit: ${JSON.stringify(result)}`)
  } catch (err) {
    await note($, `flow close failed: ${String(err)}`)
  }
}

// In flow mode nobody watches the window: a "You should know" card would wait unread, so
// ysk_forward.py presses 1 (Learn more), 2 (Chat in main session) and Enter for it.
const YSK_HELPER = 'C:/mycode/ruti/mods/ruti-seat/ysk_forward.py'
async function forwardCards($: any): Promise<void> {
  try {
    const id = await $.session.id()
    const flow = await $.process.run(['ruti', 'flow', 'active', '--session', id], { timeoutMs: 8000 })
    if (!JSON.parse(flow.stdout || '{}').flow) return
    const run = await $.process.run(['python', '-I', YSK_HELPER], { timeoutMs: 8000 })
    await note($, `ysk_forward: ${run.stdout.trim() || `exit ${run.exitCode}`}`)
  } catch (err) {
    await note($, `ysk_forward failed: ${String(err)}`)
  }
}

export const register: Register = on => {
  let pendingEffort: string | null = null
  let goalToClear = false
  let handedOff = false

  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    try {
      const args = e as unknown as Record<string, unknown>
      const command = typeof args.command === 'string' ? args.command : ''
      // A pending `/goal clear` waits for turn.complete: the host refuses command.run here.
      if (!goalToClear && HANDOFF_CALL.test(command)) {
        const text = String((result as { text?: unknown })?.text ?? '')
        if (!(result as { isError?: boolean })?.isError && text.includes(HANDOFF_WRITTEN)) {
          handedOff = true
          // command.run is refused inside tool.call; turn.complete clears it.
          goalToClear = true
        }
      }
    } catch (err) {
      await note($, `tool.call error: ${String(err)}`)
    }
    return result
  })

  on('prompt.submit', async ($, e, next) => {
    // A prompt a plugin or a peer sent is not the user's task; leave the seat alone.
    // `bridge` is the user too, typing on the phone or the web through Remote Control.
    const kind = e.origin ? (e.origin as { kind?: string }).kind : 'composer'
    if (kind !== 'composer' && kind !== 'bridge') return next(e)
    try {
      const id = await $.session.id()
      LOG = `${LOG_DIR}/ruti-seat-${String(id).slice(0, 8)}.log`
      const run = await $.process.run(['ruti', 'seat', 'plan', '--session', id], {
        stdin: e.text,
        timeoutMs: 8000,
      })
      if (run.exitCode !== 0) {
        await note($, `plan failed: exit ${run.exitCode} ${run.stderr.slice(0, 120)}`)
        return next(e)
      }
      const plan: Plan = JSON.parse(run.stdout)
      pendingEffort = null
      if (plan.apply && plan.action) {
        if (plan.action.effort) pendingEffort = plan.action.effort
        const alias = plan.action.model
        if (alias) {
          const rows = await $.config.list()
          const now = String(rows.find(r => r.key === 'model')?.value ?? '')
          const value = now.includes('[1m]') && WITH_1M.has(alias) ? `${alias}[1m]` : alias
          const kept = await snapshotDefaults($)
          const result = await $.config.set({ key: 'model', value })
          await restoreDefaults($, kept, `model -> ${value}`)
          await note($, `model ${now} -> ${value}: ${JSON.stringify(result)}`)
          $.ui.toast(`ruti: model -> ${value}`)
        }
      } else {
        await note($, `plan (${plan.mode}) best=${plan.best} apply=${plan.apply}`)
      }
    } catch (err) {
      await note($, `prompt.submit error: ${String(err)}`)
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (goalToClear) goalToClear = !(await clearGoal($, 'turn.complete'))
    await forwardCards($)
    if (handedOff) {
      handedOff = false
      await closeIfHandedOff($)
      return next(e)
    }
    if (pendingEffort) {
      await note($, `turn.complete with effort ${pendingEffort} pending: aborted=${String((e as any).isAborted)} reason=${String((e as any).reason)}`)
      const level = pendingEffort
      pendingEffort = null
      try {
        const kept = await snapshotDefaults($)
        // Started before `/effort`, awaited after it: the command does not return while its
        // dialog is up, and the helper is what takes the dialog down. Nothing may outlive
        // the hook -- a `.then` on `$` after it returned crashed the hooks worker.
        const rows = await $.config.list()
        const model = String(rows.find(r => r.key === 'model')?.value ?? '').toLowerCase()
        const helper = DIALOG_MODELS.some(m => model.includes(m))
          ? $.process.run(['python', '-I', HELPER, level, String(HELPER_TIMEOUT_S)], {
              timeoutMs: (HELPER_TIMEOUT_S + 5) * 1000,
            })
          : null
        const result = await $.command.run({ command: 'effort', args: level })
        await restoreDefaults($, kept, `/effort ${level}`)
        await note($, `/effort ${level}: ${JSON.stringify(result)}`)
        if (helper) {
          try {
            const run = await helper
            await note($, `effort_confirm (${model}): ${run.stdout.trim() || `exit ${run.exitCode}`}`)
          } catch (err) {
            await note($, `effort_confirm failed: ${String(err)}`)
          }
        }
        $.ui.toast(`ruti: effort -> ${level}`)
      } catch (err) {
        await note($, `/effort ${level} failed: ${String(err)}`)
      }
    }
    return next(e)
  })
}
