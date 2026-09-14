/**
 * The chat side panel's file-browser rail.
 *
 * Pins the parts of the rail that no other suite owns: the All/Changed segment
 * (whose mode is MODULE-level session state, so it must survive a remount), the
 * always-open search field feeding the tree's search session, the Name/Content
 * toggle and the content-search results list, the refresh escape hatch, and the
 * grip's clamp + persistence. The Pierre tree is replaced by a probe that echoes
 * the props it was handed, so "what the rail tells the tree" is assertable
 * without loading the trees runtime, and `/api/file-grep` is stubbed so the
 * results list is asserted against a known payload rather than a live walk.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  render, screen, waitFor, fireEvent, within, cleanup, renderHook,
} from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const H = vi.hoisted(() => ({
  OPENED: '/repo/src/a.ts',
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
  },
  fileGrep: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: H.api }))

vi.mock('../api/fileGrep', () => ({ fileGrep: H.fileGrep }))

vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: (p: {
    mode?: string
    projectDir: string
    searchQuery?: string | null
    selectedPath?: string | null
    onFileOpen?: (abs: string) => void
  }) => (
    <button
      data-testid="tree"
      data-mode={p.mode}
      data-dir={p.projectDir}
      data-query={p.searchQuery ?? ''}
      data-selected={p.selectedPath ?? ''}
      onClick={() => p.onFileOpen?.(H.OPENED)}
    >
      tree
    </button>
  ),
}))

import FileBrowserRail, { useTreeAvailable } from '../pages/chat/FileBrowserRail'

const RAIL_W_KEY = 'mc-files-rail-w'
const DIR = '/repo'

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function mount(props: { onFileOpen?: (p: string, d: boolean) => void; selectedPath?: string | null; projectDir?: string } = {}) {
  const qc = newClient()
  const onFileOpen = props.onFileOpen ?? vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <FileBrowserRail projectDir={props.projectDir ?? DIR} onFileOpen={onFileOpen} selectedPath={props.selectedPath} />
    </QueryClientProvider>,
  )
  return { qc, onFileOpen, ...utils }
}

const rail = () => screen.getByRole('separator').nextElementSibling as HTMLElement
const tree = () => screen.getByTestId('tree')

beforeEach(() => {
  localStorage.clear()
  H.api.projectTree.mockReset().mockResolvedValue({ root: DIR, paths: [], repo: true })
  H.api.projectGitStatus.mockReset().mockResolvedValue({ repo: true, files: [] })
  H.fileGrep.mockReset().mockResolvedValue({
    results: [], truncated: false, engine: 'rg', skipped_docs: 0, root: DIR,
  })
  // Three pieces of the rail's state are MODULE-level so that in-place tab
  // navigation (which remounts the rail) keeps them — which also means a prior
  // test's toggle or typed filter survives into this one. Drive all three back
  // to their defaults on one mount, search MODE first: the field's label is
  // mode-dependent, so the filter cannot be found until Name is showing again.
  mount()
  fireEvent.click(screen.getByLabelText('Search file names'))
  fireEvent.click(screen.getByLabelText('All files'))
  fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: '' } })
  cleanup()
  H.api.projectTree.mockClear()
  H.api.projectGitStatus.mockClear()
  H.fileGrep.mockClear()
})

describe('FileBrowserRail mode segment', () => {
  it('starts in All files mode and runs the tree in all mode', () => {
    mount()
    expect(screen.getByLabelText('All files')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'false')
    expect(tree()).toHaveAttribute('data-mode', 'all')
    expect(tree()).toHaveAttribute('data-dir', DIR)
  })

  it('switches the tree to changed mode', () => {
    mount()
    fireEvent.click(screen.getByLabelText('Changed'))
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('All files')).toHaveAttribute('aria-pressed', 'false')
    expect(tree()).toHaveAttribute('data-mode', 'changed')
  })

  it('keeps Changed mode across a remount', () => {
    mount()
    fireEvent.click(screen.getByLabelText('Changed'))
    cleanup()
    mount()
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'true')
    expect(tree()).toHaveAttribute('data-mode', 'changed')
  })

  it('badges the Changed segment with the working-tree file count', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      files: [
        { path: 'a.ts', status: 'M', staged: false },
        { path: 'b.ts', status: 'M', staged: false },
        { path: 'c.ts', status: '?', staged: false },
      ],
    })
    mount()
    expect(await within(screen.getByLabelText('Changed')).findByText('3')).toBeInTheDocument()
  })

  it('omits the badge when the working tree is clean', async () => {
    mount()
    await waitFor(() => expect(H.api.projectGitStatus).toHaveBeenCalledWith(DIR))
    expect(within(screen.getByLabelText('Changed')).queryByText('0')).toBeNull()
  })
})

describe('FileBrowserRail search field', () => {
  it('feeds the typed query to the tree and clears it from the X button', () => {
    mount()
    expect(screen.queryByLabelText('Close search')).toBeNull()
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    expect(tree()).toHaveAttribute('data-query', 'rail')
    fireEvent.click(screen.getByLabelText('Close search'))
    expect(tree()).toHaveAttribute('data-query', '')
    expect(screen.queryByLabelText('Close search')).toBeNull()
  })

  it('clears the query on Escape', () => {
    mount()
    const input = screen.getByLabelText('Filter files…')
    fireEvent.change(input, { target: { value: 'rail' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveValue('')
    expect(tree()).toHaveAttribute('data-query', '')
  })

  it('leaves the query alone on any other key', () => {
    mount()
    const input = screen.getByLabelText('Filter files…')
    fireEvent.change(input, { target: { value: 'rail' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('rail')
  })

  it('keeps the typed query across a remount of the same project directory', () => {
    // In-place tab navigation (opening a file focuses its tab) remounts the
    // rail; the filter survives through the module-level session map, the
    // same way the All/Changed mode does.
    mount()
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    cleanup()
    mount()
    expect(screen.getByLabelText('Filter files…')).toHaveValue('rail')
    expect(tree()).toHaveAttribute('data-query', 'rail')
  })

  it('rehydrates the query when the mounted rail switches project directory', () => {
    // An in-place projectDir change (switching chat slots) must not carry the
    // previous project's filter into the new one — the seed read happens only
    // on first mount, so the rail rehydrates from the session map on change.
    const qc = newClient()
    const view = render(
      <QueryClientProvider client={qc}>
        <FileBrowserRail projectDir={DIR} onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    view.rerender(
      <QueryClientProvider client={qc}>
        <FileBrowserRail projectDir="/elsewhere-switched" onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    expect(screen.getByLabelText('Filter files…')).toHaveValue('')
    expect(tree()).toHaveAttribute('data-query', '')

    // And switching back restores the first project's filter from the map.
    view.rerender(
      <QueryClientProvider client={qc}>
        <FileBrowserRail projectDir={DIR} onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    expect(screen.getByLabelText('Filter files…')).toHaveValue('rail')
  })

  it('starts a different project directory with an empty query', () => {
    mount()
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    cleanup()
    mount({ projectDir: '/elsewhere' })
    expect(screen.getByLabelText('Filter files…')).toHaveValue('')
    expect(tree()).toHaveAttribute('data-query', '')
  })
})

describe('FileBrowserRail file opens', () => {
  it('opens a plain file in All mode and keeps the query', () => {
    // Deliberate contract: opening a file keeps the filter, so a second file
    // can be opened from the same filtered result without re-typing it.
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'a.ts' } })
    fireEvent.click(tree())
    expect(onFileOpen).toHaveBeenCalledWith(H.OPENED, false)
    expect(tree()).toHaveAttribute('data-query', 'a.ts')
  })

  it('opens in diff mode from Changed mode', () => {
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    fireEvent.click(screen.getByLabelText('Changed'))
    fireEvent.click(tree())
    expect(onFileOpen).toHaveBeenCalledWith(H.OPENED, true)
  })

  it('echoes the host selection into the tree', () => {
    mount({ selectedPath: '/repo/src/b.ts' })
    expect(tree()).toHaveAttribute('data-selected', '/repo/src/b.ts')
  })

  it('passes an empty selection when no file is open', () => {
    mount()
    expect(tree()).toHaveAttribute('data-selected', '')
  })
})

describe('FileBrowserRail refresh', () => {
  it('refetches both polling queries and locks the button until they land', async () => {
    const { qc } = mount()
    const releases: Array<() => void> = []
    const spy = vi
      .spyOn(qc, 'refetchQueries')
      .mockImplementation((() =>
        new Promise<void>(res => { releases.push(res) })) as typeof qc.refetchQueries)

    const btn = screen.getByLabelText('Refresh')
    fireEvent.click(btn)

    expect(spy.mock.calls.map(c => (c[0] as { queryKey: unknown[] }).queryKey)).toEqual([
      ['project-tree', DIR],
      ['git-status', DIR],
      // Content results are cached, so the escape hatch has to reach them too:
      // a refresh that left a stale hit list beside a fresh tree would present
      // two different answers to one query.
      ['file-grep', DIR],
    ])
    await waitFor(() => expect(btn).toBeDisabled())
    expect(btn.querySelector('svg')?.getAttribute('class')).toContain('animate-spin')

    releases.forEach(r => r())
    await waitFor(() => expect(btn).not.toBeDisabled())
    expect(btn.querySelector('svg')?.getAttribute('class')).not.toContain('animate-spin')
  })
})

describe('FileBrowserRail resize grip', () => {
  it('grows leftward, clamps to both bounds, and persists the release width', () => {
    mount()
    const grip = screen.getByRole('separator')
    expect(grip).toHaveAttribute('aria-orientation', 'vertical')
    // First run has nothing stored, so the rail opens at its own minimum —
    // never below it, which would snap wider on the first drag.
    expect(rail().style.width).toBe('300px')

    fireEvent.pointerDown(grip, { clientX: 500, clientY: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')

    // The grip sits on the rail's LEFT edge, so a leftward drag grows it.
    fireEvent.pointerMove(grip, { clientX: 400, clientY: 0, pointerId: 1 })
    expect(rail().style.width).toBe('400px')

    fireEvent.pointerMove(grip, { clientX: 900, clientY: 0, pointerId: 1 })
    expect(rail().style.width).toBe('300px')

    fireEvent.pointerMove(grip, { clientX: 100, clientY: 0, pointerId: 1 })
    expect(rail().style.width).toBe('520px')

    fireEvent.pointerUp(grip, { clientX: 100, clientY: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
    expect(localStorage.getItem(RAIL_W_KEY)).toBe('520')
  })

  it('restores the body styles when unmounted mid-drag', () => {
    const { unmount } = mount()
    fireEvent.pointerDown(screen.getByRole('separator'), { clientX: 500, clientY: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('col-resize')
    unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })

  it.each([
    ['9999', '520px'],
    ['10', '300px'],
    ['360', '360px'],
    ['not-a-number', '300px'],
  ])('seeds the width from a stored %s as %s', (stored, expected) => {
    localStorage.setItem(RAIL_W_KEY, stored)
    mount()
    expect(rail().style.width).toBe(expected)
  })
})

describe('useTreeAvailable', () => {
  const wrapper = (qc: QueryClient) => ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )

  it('reports available while the tree endpoint answers', async () => {
    const qc = newClient()
    const { result } = renderHook(() => useTreeAvailable(DIR), { wrapper: wrapper(qc) })
    await waitFor(() =>
      expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('success'))
    expect(result.current).toBe(true)
    expect(H.api.projectTree).toHaveBeenCalledWith(DIR)
  })

  it('reports unavailable once the tree endpoint errors', async () => {
    H.api.projectTree.mockRejectedValue(new Error('not a directory'))
    const { result } = renderHook(() => useTreeAvailable(DIR), { wrapper: wrapper(newClient()) })
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('never probes without a project directory', () => {
    const { result } = renderHook(() => useTreeAvailable(null), { wrapper: wrapper(newClient()) })
    expect(result.current).toBe(false)
    expect(H.api.projectTree).not.toHaveBeenCalled()
  })
})

/** A payload with one text hit and one document hit — the two row shapes. */
const GREP_PAYLOAD = {
  results: [
    { file: '/repo/src/a.ts', line: 12, preview: 'const NEEDLE = 1' },
    { file: '/repo/docs/spec.pptx', line: 0, preview: 'the needle roadmap', label: 'slide 7' },
  ],
  truncated: false,
  engine: 'rg' as const,
  skipped_docs: 0,
  root: DIR,
}

/** Switch to Content mode and type *query*, then wait for the debounced fetch. */
async function searchContents(query: string) {
  fireEvent.click(screen.getByLabelText('Search file contents'))
  fireEvent.change(screen.getByLabelText('Search in files…'), { target: { value: query } })
  await waitFor(() => expect(H.fileGrep).toHaveBeenCalled())
}

describe('FileBrowserRail search-mode toggle', () => {
  it('starts on Name, where the query filters the tree and no content search runs', () => {
    mount()
    expect(screen.getByLabelText('Search file names')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Search file contents')).toHaveAttribute('aria-pressed', 'false')
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'needle' } })
    expect(tree()).toHaveAttribute('data-query', 'needle')
    expect(H.fileGrep).not.toHaveBeenCalled()
  })

  it('replaces the tree with content results in Content mode', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    // The tree is not merely hidden: the rail has one body, and a stale tree
    // beside a hit list would leave two competing answers to one query.
    await waitFor(() => expect(screen.queryByTestId('tree')).toBeNull())
  })

  it('keeps Content mode across a remount', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    fireEvent.click(screen.getByLabelText('Search file contents'))
    cleanup()
    mount()
    expect(screen.getByLabelText('Search file contents')).toHaveAttribute('aria-pressed', 'true')
  })

  it('asks for nothing below the two-character floor', async () => {
    mount()
    fireEvent.click(screen.getByLabelText('Search file contents'))
    fireEvent.change(screen.getByLabelText('Search in files…'), { target: { value: 'n' } })
    // The hint stands in for the results list, so an empty rail never reads as
    // "searched and found nothing".
    expect(screen.getByText(/at least two characters/i)).toBeInTheDocument()
    await new Promise(r => setTimeout(r, 320))
    expect(H.fileGrep).not.toHaveBeenCalled()
  })

  it('debounces to ONE request for a burst of keystrokes', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    fireEvent.click(screen.getByLabelText('Search file contents'))
    const field = screen.getByLabelText('Search in files…')
    for (const value of ['ne', 'nee', 'need', 'needl', 'needle']) {
      fireEvent.change(field, { target: { value } })
    }
    await waitFor(() => expect(H.fileGrep).toHaveBeenCalledTimes(1))
    expect(H.fileGrep.mock.calls[0][0]).toBe(DIR)
    expect(H.fileGrep.mock.calls[0][1]).toBe('needle')
  })
})

describe('FileBrowserRail content results', () => {
  it('shortens each path against the searched root and marks the match', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    expect(await screen.findByText('src/a.ts')).toBeInTheDocument()
    expect(screen.getByText('docs/spec.pptx')).toBeInTheDocument()
    // The file's own casing survives: the run is located case-insensitively but
    // sliced out of the original text.
    expect(rail().querySelector('mark')?.textContent).toBe('NEEDLE')
  })

  it('shows :line for a text hit and the location badge for a document hit', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    expect(await screen.findByText(':12')).toBeInTheDocument()
    // A .pptx has no line to open at, so the row names the slide instead.
    expect(screen.getByText('slide 7')).toBeInTheDocument()
  })

  it('opens a text hit at its line', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    await searchContents('needle')
    fireEvent.click(await screen.findByText('src/a.ts'))
    expect(onFileOpen).toHaveBeenCalledWith('/repo/src/a.ts', false, { line: 12 })
  })

  it('opens a document hit without a line to reveal', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    await searchContents('needle')
    fireEvent.click(await screen.findByText('docs/spec.pptx'))
    // Line 0 is not a line: asking the panel to reveal it would scroll to a
    // position the file does not have.
    expect(onFileOpen).toHaveBeenCalledWith('/repo/docs/spec.pptx', false, undefined)
  })

  it('reports the count, and keeps the engine name out of the line', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    const status = await waitFor(() => {
      const el = screen.getByTestId('file-grep-status')
      expect(el.textContent).toMatch(/2 result/i)
      return el
    })
    // "python" in the visible line was read as "the search was narrowed to
    // Python files", i.e. as a filter that made the list incomplete. The engine
    // is diagnostic, so it answers "why was that slow" from the tooltip instead.
    expect(status.textContent).not.toContain('rg')
    expect(status).toHaveAttribute('title', expect.stringContaining('rg'))
  })

  it('says the search was capped rather than presenting a partial list as complete', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, truncated: true, engine: 'python' })
    mount()
    await searchContents('needle')
    const status = await waitFor(() => {
      const el = screen.getByTestId('file-grep-status')
      expect(el.textContent).toContain('capped')
      return el
    })
    // The remedy is visible and keyboard-readable; only the engine stays in
    // the diagnostic tooltip.
    expect(await screen.findByText(/Narrow the query/i)).toBeVisible()
    expect(status).toHaveAttribute('title', expect.stringContaining('python'))
  })

  it('names BOTH modes in words, whichever is active', async () => {
    mount()
    // An icon pair was misread; an icon pair with only the active word was
    // still misread. Two plain words on their own row: nothing left to guess,
    // and the search field keeps its full width for the query.
    expect(screen.getByLabelText('Search file names')).toHaveTextContent('Name')
    expect(screen.getByLabelText('Search file contents')).toHaveTextContent('Content')
    fireEvent.click(screen.getByLabelText('Search file contents'))
    expect(screen.getByLabelText('Search file names')).toHaveTextContent('Name')
    expect(screen.getByLabelText('Search file contents')).toHaveTextContent('Content')
    // The toggle is no longer INSIDE the search field.
    const field = screen.getByLabelText('Search in files…').parentElement as HTMLElement
    expect(within(field).queryByLabelText('Search file names')).toBeNull()
  })

  it('says visibly, once, that non-PDF document hits open from the start', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    // A `slide 7` badge reads as a jump target the click cannot honour. The
    // correction is visible text above the list -- not a hover tooltip, which
    // has no affordance and no keyboard path.
    await screen.findByText('slide 7')
    expect(screen.getByTestId('file-grep-doc-note')).toHaveTextContent(/open the document from the start/)
  })

  it('opens a PDF hit at its page, through the same reveal channel as a line', async () => {
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [{ file: '/repo/docs/spec.pdf', line: 0, preview: 'needle on page three', label: 'p 3' }],
    })
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    await searchContents('needle')
    fireEvent.click(await screen.findByText('docs/spec.pdf'))
    // `p 3` is the one document location a viewer can honour (#page=3), so the
    // click carries it in the reveal slot the panel already has.
    expect(onFileOpen).toHaveBeenCalledWith('/repo/docs/spec.pdf', false, { line: 3 })
    // ... and a PDF-only result set needs no "opens from the start" note.
    expect(screen.queryByTestId('file-grep-doc-note')).toBeNull()
  })

  it('names the documents the budget did not reach', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, skipped_docs: 4 })
    mount()
    await searchContents('needle')
    await waitFor(() =>
      expect(screen.getByTestId('file-grep-status').textContent)
        .toContain('documents not searched: 4'))
  })

  it('reads correctly for a single skipped document', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, skipped_docs: 1 })
    mount()
    await searchContents('needle')
    // The count follows a label rather than inflecting a noun: "1 documents not
    // searched" was the bug, and a colon form has no agreement to get wrong in
    // any of the twelve catalogs.
    await waitFor(() =>
      expect(screen.getByTestId('file-grep-status').textContent)
        .toContain('documents not searched: 1'))
  })

  it('reports an empty answer as no matches, not as an empty rail', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, results: [] })
    mount()
    await searchContents('needle')
    expect(await screen.findByText('No matches')).toBeInTheDocument()
  })

  it('says the search failed instead of showing zero results', async () => {
    H.fileGrep.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    mount()
    await searchContents('needle')
    // A refused root and an empty tree are different facts, and the second is
    // the one a user would act on by editing their query.
    const notice = await screen.findByTestId('file-grep-error')
    expect(notice).toHaveTextContent('Content search failed')
    // Through ErrorNotice, not a hand-written red line: that is the one surface
    // carrying the agent hand-off for an error the user cannot fix by retyping.
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.getByTestId('file-grep-status')).not.toHaveTextContent('failed')
  })

  it('offers the agent hand-off on a failure, and no result rows', async () => {
    H.fileGrep.mockRejectedValue(Object.assign(new Error('nope'), { status: 503 }))
    mount()
    await searchContents('needle')
    await screen.findByTestId('file-grep-error')
    // An exhausted probe pool is the agent's problem, not the user's, so the
    // list stays empty rather than showing a stale hit set beside the error.
    expect(screen.queryByText('src/a.ts')).toBeNull()
    expect(screen.queryByText('No matches')).toBeNull()
  })
})
