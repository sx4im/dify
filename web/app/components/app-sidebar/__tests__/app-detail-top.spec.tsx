import { Dialog, DialogPopup, DialogPortal, DialogTitle } from '@langgenius/dify-ui/dialog'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { gotoAnythingDialogHandle } from '@/app/components/goto-anything/handles'
import { renderWithConsoleQuery as render } from '@/test/console/query-data'
import { AppDetailTop } from '../app-detail-top'

function TestGotoAnythingDialog() {
  return (
    <Dialog handle={gotoAnythingDialogHandle}>
      <DialogPortal>
        <DialogPopup>
          <DialogTitle>Goto Anything</DialogTitle>
        </DialogPopup>
      </DialogPortal>
    </Dialog>
  )
}

function AppSidebarTopOwner() {
  const [expanded, setExpanded] = useState(true)
  return <AppDetailTop expand={expanded} onToggle={() => setExpanded((value) => !value)} />
}

describe('AppDetailTop', () => {
  beforeEach(() => {
    gotoAnythingDialogHandle.close()
  })

  it('links the breadcrumb to Home and Studio', () => {
    render(<AppDetailTop />)

    expect(screen.getByRole('link', { name: 'navigation.mainNav.home' })).toHaveAttribute(
      'href',
      '/',
    )
    expect(screen.getByRole('link', { name: 'navigation.menus.apps' })).toHaveAttribute(
      'href',
      '/apps',
    )
    expect(screen.queryByRole('button', { name: 'common.operation.back' })).not.toBeInTheDocument()
  })

  it('opens quick search from the app sidebar', async () => {
    const user = userEvent.setup()
    render(
      <>
        <AppDetailTop />
        <TestGotoAnythingDialog />
      </>,
    )
    expect(screen.queryByRole('dialog', { name: 'Goto Anything' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'app.gotoAnything.searchTitle' }))

    expect(await screen.findByRole('dialog', { name: 'Goto Anything' })).toBeInTheDocument()
  })

  it('offers collapse from the expanded sidebar', async () => {
    const user = userEvent.setup()
    const onToggle = vi.fn()

    render(<AppDetailTop onToggle={onToggle} />)
    await user.click(screen.getByRole('button', { name: 'layout.sidebar.collapseSidebar' }))

    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('keeps keyboard focus on the toggle when collapsing and expanding', async () => {
    const user = userEvent.setup()
    render(<AppSidebarTopOwner />)

    await user.click(screen.getByRole('button', { name: 'layout.sidebar.collapseSidebar' }))
    const expand = screen.getByRole('button', { name: 'layout.sidebar.expandSidebar' })
    expect(expand).toHaveFocus()

    await user.click(expand)
    expect(screen.getByRole('button', { name: 'layout.sidebar.collapseSidebar' })).toHaveFocus()
    expect(screen.getByRole('link', { name: 'navigation.menus.apps' })).toBeVisible()
  })
})
