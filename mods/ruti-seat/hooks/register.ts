import type { Register } from 'claude-code'

// ruti decides; this mod only carries the decision out. On a fresh prompt it asks
// `ruti seat plan` (which classifies the prompt and applies the policy: up at once,
// down only with good reason, model switches only when cheap) and
//   * sets the model right away -- `$.config.set` is allowed inside the prompt hook;
//   * keeps the effort for `turn.complete` -- `/effort` is refused while the hook holds
//     the turn, so it takes effect for the turn after.
// In `shadow` mode the plan comes back with apply=false and nothing is touched.

const LOG = 'C:/Users/Lenovo Gaming/.claude/dev-mods/ruti-seat.log'
const WITH_1M = new Set(['sonnet', 'opus', 'fable'])

type Plan = { apply?: boolean; action?: { model?: string; effort?: string }; best?: string; mode?: string }

const lines: string[] = []

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
          const result = await $.config.set({ key: 'model', value })
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
        const result = await $.command.run({ command: 'effort', args: level })
        await note($, `/effort ${level}: ${JSON.stringify(result)}`)
        $.ui.toast(`ruti: effort -> ${level}`)
      } catch (err) {
        await note($, `/effort ${level} failed: ${String(err)}`)
      }
    }
    return next(e)
  })
}
