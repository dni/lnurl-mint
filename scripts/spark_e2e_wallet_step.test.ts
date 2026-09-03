// E2E step dispatcher: runs ONE holder-side LUD-25 step using lnurl-wallet's
// real protocol module (src/lnurlcash.ts - the exact functions Mint.tsx /
// MeltDialog.tsx / Wallet.tsx drive), so the spark backend's e2e exercises
// the reference wallet implementation itself, not a re-implementation.
//
// This is the canonical copy; scripts/spark_e2e.py copies it into the
// lnurl-wallet repo (see --wallet-dir) as src/__e2e_step__.test.ts and runs
// it under vitest once per step (vite resolves the wallet's TS imports),
// removing it again at the end. Channels are files, not env: vitest workers
// don't inherit the parent environment.
//
//   plan (E2E_STEP_FILE, default /tmp/e2e_step.json):
//     {"step": "<name>", "args": [...]}
//   result (E2E_RESULT_FILE, default /tmp/e2e_result.json):
//     {"ok": true, "result": <json>} or {"ok": false, "error": "<msg>"}
//
// The localStorage shim is FILE-backed (/tmp/e2e_wallet_storage.json) so
// cashSecrets' per-domain secret index advances across runs exactly like a
// real browser's would - a fresh run otherwise re-derives secret #0, and
// the mint (correctly) rejects a reused comment hash. Secrets live only in
// these throwaway files and the orchestrator's state, never committed.
import {expect, test} from 'vitest'
import {readFileSync, writeFileSync} from 'node:fs'

const STORAGE_FILE = '/tmp/e2e_wallet_storage.json'
const storage: Record<string, string> = (() => {
  try {
    return JSON.parse(readFileSync(STORAGE_FILE, 'utf8'))
  } catch {
    return {}
  }
})()
;(globalThis as any).localStorage = {
  getItem: (k: string) => (k in storage ? storage[k] : null),
  setItem: (k: string, v: unknown) => {
    storage[k] = String(v)
    writeFileSync(STORAGE_FILE, JSON.stringify(storage))
  },
  removeItem: (k: string) => {
    delete storage[k]
    writeFileSync(STORAGE_FILE, JSON.stringify(storage))
  },
  clear: () => {
    for (const k of Object.keys(storage)) delete storage[k]
    writeFileSync(STORAGE_FILE, JSON.stringify(storage))
  }
}

const {
  fetchPayRequest,
  requestInvoice,
  fetchInvoiceVerification,
  fetchNoteInfo,
  rotateNote,
  splitNote,
  mergeNotes,
  meltNote,
  generateMintSecret,
  hashK1
} = await import('./lnurlcash')
const {deriveLud25CashRootNode} = await import('./keys')
const {setCashRoot} = await import('./cashSecrets')

// "unlock" exactly the way WalletContext's activate path does
setCashRoot(
  deriveLud25CashRootNode(
    process.env.E2E_WALLET_SEED ??
      'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about'
  )
)

const plan = JSON.parse(
  readFileSync(process.env.E2E_STEP_FILE ?? '/tmp/e2e_step.json', 'utf8')
)
const step: string = plan.step
const args: string[] = plan.args
const RESULT_FILE = process.env.E2E_RESULT_FILE ?? '/tmp/e2e_result.json'

test(`e2e step: ${step}`, async () => {
  try {
    let result: unknown
    switch (step) {
      case 'pay-request':
        result = await fetchPayRequest(args[0])
        break
      case 'mint': {
        // exactly what Mint.tsx does: seed-derived secret, disclosed only
        // as its hash via the mandatory LUD-12 comment
        const secret = generateMintSecret(new URL(args[0]).host)
        const invoice = await requestInvoice(args[0], Number(args[1]), hashK1(secret))
        result = {pr: invoice.pr, verify: invoice.verify, secret}
        break
      }
      case 'verify':
        result = await fetchInvoiceVerification(args[0])
        break
      case 'note-info': {
        const info = await fetchNoteInfo(args[0])
        result = {maxWithdrawable: info.maxWithdrawable, min: info.minWithdrawable}
        break
      }
      case 'rotate':
        result = await rotateNote(args[0], args[1])
        break
      case 'split':
        result = await splitNote(args[0], [args[1]], Number(args[2]))
        break
      case 'merge':
        result = await mergeNotes(args[0], args.slice(1))
        break
      case 'melt':
        result = await meltNote(args[0], args[1], args[2])
        break
      default:
        throw new Error(`unknown step ${step}`)
    }
    writeFileSync(RESULT_FILE, JSON.stringify({ok: true, result}))
    expect(result).toBeTruthy()
  } catch (err) {
    // expected failures (negative cases) are signaled via plan.expectError
    const message = err instanceof Error ? err.message : String(err)
    writeFileSync(
      RESULT_FILE,
      JSON.stringify({ok: false, error: message, expected: plan.expectError === message})
    )
    if (plan.expectError !== undefined) {
      expect(message).toBe(plan.expectError)
    } else {
      throw err
    }
  }
})
