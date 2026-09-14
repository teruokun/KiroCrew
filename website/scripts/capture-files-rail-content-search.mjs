/**
 * Real-browser evidence for the Files rail's CONTENT search.
 *
 * Drives the isolated capture entry (website/capture/files-rail-content-search.html),
 * which mounts the REAL `FileBrowserRail` with `/api/file-grep` stubbed at the
 * fetch boundary. The mode toggle is CLICKED here rather than seeded, so each
 * frame shows a state a user actually reaches.
 *
 * The unit suite (src/test/FileBrowserRail.test.tsx) pins the same behaviour in
 * happy-dom. This exists for what a DOM assertion cannot carry: whether the row
 * reads at rail width — a path, a `:line` or a location badge, and a preview
 * with the match marked, inside 300-520px.
 *
 * Frames per theme:
 *   01-name-mode        the filename filter, unchanged, for contrast
 *   02-content-results  text hits plus three document hits with their badges
 *   03-content-capped   the status line's other arm: capped, python, 6 skipped
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6837 --strictPort   # in another shell (website/)
 *   node scripts/capture-files-rail-content-search.mjs http://127.0.0.1:6837 ../temp-screenshots/files-rail-content-search
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6837'
const OUT = process.argv[3] || '../temp-screenshots/files-rail-content-search'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 520, height: 760 }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

/** Every frame this run produces: [file suffix, capped arm, what to do]. */
const FRAMES = [
  ['01-name-mode', 'off', 'name'],
  ['02-content-results', 'off', 'content'],
  ['03-content-capped', 'on', 'content'],
]

for (const theme of ['light', 'dark']) {
  for (const [name, capped, mode] of FRAMES) {
    const page = await browser.newPage({ viewport: VIEWPORT })
    page.on('pageerror', e => { console.error(`[${theme}/${name}] pageerror:`, e.message); failures++ })
    await page.goto(
      `${BASE}/capture/files-rail-content-search.html?theme=${theme}&capped=${capped}`,
      { waitUntil: 'networkidle' },
    )

    let observed = ''
    if (mode === 'content') {
      await page.getByLabel('Search file contents').click()
      await page.getByLabel('Search in files…').fill('rate limit')
      // Wait for the rail's own settled shape rather than a fixed sleep: the
      // status line is the last thing to resolve.
      await page.waitForFunction(
        () => (document.querySelector('[data-testid="file-grep-status"]')?.textContent || '')
          .includes('result'),
        undefined,
        { timeout: 15000 },
      )
      observed = (await page.getByTestId('file-grep-status').textContent()) || ''
    } else {
      // Name mode must still be the tree, not a hit list.
      await page.getByLabel('Filter files…').fill('limits')
      await page.waitForTimeout(300)
    }
    await page.waitForTimeout(150)

    // The palette is asserted, not assumed: a filename claiming a theme the
    // frame does not carry is exactly the failure this catches.
    const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    const themeOk = applied === (theme === 'light' ? 'kiro-light' : 'kiro-dark')
    await page.screenshot({ path: `${OUT}/${theme}-${name}.png` })

    // The badges are the point of the document pass, so their presence is
    // asserted rather than left to a reader of the image. Matched as whole
    // element text, not against the row: a row concatenates the path and the
    // badge with no separator, so `docs/spec.pdf` + `p 3` reads as `pdfp 3` and
    // any word-boundary pattern over the row silently finds nothing.
    const badges = await page.evaluate(() =>
      [...document.querySelectorAll('span')]
        .map(el => (el.textContent || '').trim())
        .filter(text => /^(p \d+|slide \d+|\S+ r\d+)$/.test(text)).length)
    // Every fixture preview contains the query, so every row must show a marked
    // run. A row without one is what made the reader read code hits and document
    // hits as two different kinds of "match".
    const marks = await page.evaluate(() => document.querySelectorAll('mark').length)

    let ok = themeOk
    if (mode === 'content' && capped === 'off') {
      // The engine name is NOT in the status line any more: it read as "the
      // search was narrowed to Python files". It lives in the row's tooltip.
      ok = ok && badges === 3 && !observed.includes('capped') && !observed.includes('rg')
    } else if (mode === 'content') {
      ok = ok && observed.includes('capped') && observed.includes('not searched')
    } else {
      ok = ok && (await page.getByLabel('Search file names').getAttribute('aria-pressed')) === 'true'
    }
    if (mode === 'content') ok = ok && marks === (capped === 'on' ? 4 : 7)
    // The visible note about non-PDF document hits: present with the deck and
    // workbook hits (results arm), absent when the capped payload holds only
    // text hits.
    const note = await page.locator('[data-testid="file-grep-doc-note"]').count()
    if (mode === 'content') ok = ok && note === (capped === 'on' ? 0 : 1)
    console.log(`[${theme}/${name}] data-theme=${applied} badges=${badges} marks=${marks} `
      + `status=${JSON.stringify(observed)} => ${ok ? 'OK' : 'FAIL'}`)
    if (!ok) failures++
    await page.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}`)
