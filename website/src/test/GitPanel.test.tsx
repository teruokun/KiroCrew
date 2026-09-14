import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const H = vi.hoisted(() => ({
  api: {
    projectGitStatus: vi.fn(),
    projectGitLog: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api }))

import GitPanel from '../components/GitPanel'

const PROJECT = '/workspace/project'

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <GitPanel projectDir={PROJECT} onClose={vi.fn()} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  H.api.projectGitStatus.mockReset().mockResolvedValue({
    repo: true,
    repoRoot: PROJECT,
    branch: 'main',
    files: [],
  })
  H.api.projectGitLog.mockReset().mockResolvedValue({ repo: true, commits: [] })
})

describe('GitPanel repository state', () => {
  it('renders a terminal no-repository state without claiming the tree is clean', async () => {
    H.api.projectGitStatus.mockResolvedValue({ repo: false, files: [] })
    H.api.projectGitLog.mockResolvedValue({ repo: false, commits: [] })

    mount()

    expect(await screen.findByText('Not a Git repository')).toBeInTheDocument()
    expect(screen.getByText(
      'This project folder is not a Git working tree. If the repository is in a subfolder, set that folder as the session project.',
    )).toBeInTheDocument()
    expect(screen.queryByText('loading...')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText(/uncommitted/)).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('renders an operational status failure through ErrorNotice without a health claim', async () => {
    H.api.projectGitStatus.mockRejectedValue(new Error('status unavailable'))

    mount()

    // GitPanel retries a failed status read once before React Query exposes
    // statusError, so wait for that bounded request chain rather than the
    // default one-second query timeout.
    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('keeps the clean-repository label, badge, and empty state', async () => {
    mount()

    expect(await screen.findByText('main')).toBeInTheDocument()
    expect(screen.getByText('clean')).toBeInTheDocument()
    expect(screen.getByText('No changes or commits to display.')).toBeInTheDocument()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
  })

  it('keeps the dirty-repository label, count, and changed-file row', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      repoRoot: PROJECT,
      branch: 'feature',
      files: [{ path: 'src/a.ts', status: 'M', staged: false }],
    })

    mount()

    expect(await screen.findByText('feature')).toBeInTheDocument()
    expect(screen.getByText('1 uncommitted')).toBeInTheDocument()
    expect(screen.getByText('Changes')).toBeInTheDocument()
    expect(screen.getByTitle('src/a.ts')).toBeInTheDocument()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
  })
})
