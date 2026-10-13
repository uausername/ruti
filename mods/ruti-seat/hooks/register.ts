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

const LOG = 'C:/Users/Lenovo Gaming/.claude/dev-mods/ruti-seat.log'
const WITH_1M = new Set(['sonnet', 'opus', 'fable'])

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

export const register: Register = on => {
  let pendingEffort: string | null = null

  on('prompt.submit', async ($, e, next) => {
    // A prompt a plugin or a peer sent is not the user's task; leave the seat alone.
    if (e.origin && (e.origin as { kind?: string }).kind !== 'composer') return next(e)
    try {
      const id = await $.session.id()
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
    if (pendingEffort) {
      const level = pendingEffort
      pendingEffort = null
      try {
        const kept = await snapshotDefaults($)
        const result = await $.command.run({ command: 'effort', args: level })
        await restoreDefaults($, kept, `/effort ${level}`)
        await note($, `/effort ${level}: ${JSON.stringify(result)}`)
        $.ui.toast(`ruti: effort -> ${level}`)
      } catch (err) {
        await note($, `/effort ${level} failed: ${String(err)}`)
      }
    }
    return next(e)
  })
}
