import { render } from 'vitest-browser-react'
import StreamdownWrapper from '../streamdown-wrapper'

vi.mock('@/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config')>()),
  ALLOW_UNSAFE_DATA_SCHEME: true,
}))

// 1x1 transparent PNG as a data: URI, the same shape as the base64 Dify icon
// used to verify feature PR #21321 ("allow rendering data uri scheme").
const DATA_IMAGE =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8Xw8AAwMCAO+ip1sAAAAASUVORK5CYII='

describe('StreamdownWrapper with ALLOW_UNSAFE_DATA_SCHEME on', () => {
  it('renders a data: image instead of stripping it (issue #45920)', async () => {
    const screen = await render(<StreamdownWrapper latexContent={`![red dot](${DATA_IMAGE})`} />)

    await expect.element(screen.getByTestId('gallery-image')).toHaveAttribute('src', DATA_IMAGE)
  })
})
