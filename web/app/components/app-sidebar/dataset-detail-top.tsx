'use client'

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbSeparator,
} from '@langgenius/dify-ui/breadcrumb'
import { DialogTrigger } from '@langgenius/dify-ui/dialog'
import { Kbd, KbdGroup } from '@langgenius/dify-ui/kbd'
import { Tooltip, TooltipContent, TooltipTrigger } from '@langgenius/dify-ui/tooltip'
import { formatForDisplay } from '@tanstack/react-hotkeys'
import { useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { DetailSidebarToggleButton } from '@/app/components/detail-sidebar/toggle-button'
import { gotoAnythingDialogHandle } from '@/app/components/goto-anything/handles'
import { GOTO_ANYTHING_HOTKEY } from '@/app/components/goto-anything/hotkeys'
import Link from '@/next/link'

type DatasetDetailTopProps = {
  expand?: boolean
  onToggle?: () => void
}

export function DatasetDetailTop({ expand = true, onToggle }: DatasetDetailTopProps) {
  const { t } = useTranslation(['app', 'navigation'])
  const collapseButtonRef = useRef<HTMLButtonElement>(null)
  const expandButtonRef = useRef<HTMLButtonElement>(null)
  // The toggle unmounts when the sidebar flips, so remember which toggle held
  // focus before the flip and hand focus to its replacement when it mounts.
  const pendingFocusRef = useRef<'collapse' | 'expand' | null>(null)

  const handleToggle = useCallback(() => {
    if (document.activeElement === collapseButtonRef.current)
      pendingFocusRef.current = 'expand'
    else if (document.activeElement === expandButtonRef.current)
      pendingFocusRef.current = 'collapse'
    else
      pendingFocusRef.current = null
    onToggle?.()
  }, [onToggle])

  const focusCollapseButton = useCallback((node: HTMLButtonElement | null) => {
    collapseButtonRef.current = node
    if (node && pendingFocusRef.current === 'collapse') {
      node.focus()
      pendingFocusRef.current = null
    }
  }, [])

  const focusExpandButton = useCallback((node: HTMLButtonElement | null) => {
    expandButtonRef.current = node
    if (node && pendingFocusRef.current === 'expand') {
      node.focus()
      pendingFocusRef.current = null
    }
  }, [])

  if (!expand) {
    return (
      <div className="flex w-full items-center justify-center px-3 pt-2 pb-1">
        {onToggle && (
          <DetailSidebarToggleButton
            ref={focusExpandButton}
            expand={expand}
            onToggle={handleToggle}
            icon={
              <span aria-hidden className="i-custom-vender-line-arrows-sidebar-left-arrow size-4" />
            }
          />
        )}
      </div>
    )
  }

  return (
    <div className="flex items-center py-2 ps-1 pe-2">
      <Breadcrumb
        aria-label={t(($) => $['menus.datasets'], { ns: 'navigation' })}
        className="flex-1"
      >
        <BreadcrumbList className="gap-px">
          <BreadcrumbItem className="shrink-0">
            <BreadcrumbLink
              render={<Link href="/" />}
              aria-label={t(($) => $['mainNav.home'], { ns: 'navigation' })}
              className="gap-0 rounded-lg py-2 ps-0.5 pe-1.5 hover:bg-background-default-hover"
            >
              <span aria-hidden className="i-ri-arrow-left-s-line size-4" />
              <span aria-hidden className="i-custom-vender-main-nav-app-home size-4" />
            </BreadcrumbLink>
          </BreadcrumbItem>
          {expand && (
            <>
              <BreadcrumbSeparator className="system-md-regular" />
              <BreadcrumbItem className="shrink-0">
                <BreadcrumbLink
                  render={<Link href="/datasets" />}
                  className="rounded-lg px-1.5 py-2 system-sm-semibold-uppercase text-text-secondary hover:bg-background-default-hover hover:text-text-primary"
                >
                  {t(($) => $['menus.datasets'], { ns: 'navigation' })}
                </BreadcrumbLink>
              </BreadcrumbItem>
            </>
          )}
        </BreadcrumbList>
      </Breadcrumb>
      {expand && (
        <Tooltip>
          <TooltipTrigger
            render={
              <DialogTrigger
                handle={gotoAnythingDialogHandle}
                render={
                  <button
                    type="button"
                    aria-label={t(($) => $['gotoAnything.searchTitle'], { ns: 'app' })}
                    className="flex size-8 shrink-0 items-center justify-center overflow-hidden rounded-[10px] text-text-tertiary transition-colors hover:bg-state-base-hover hover:text-text-secondary"
                  >
                    <span aria-hidden className="i-custom-vender-main-nav-quick-search size-4" />
                  </button>
                }
              />
            }
          />
          <TooltipContent side="bottom" className="flex items-center gap-1">
            <span className="px-0.5">{t(($) => $['gotoAnything.quickAction'], { ns: 'app' })}</span>
            <KbdGroup>
              {formatForDisplay(GOTO_ANYTHING_HOTKEY, { parts: true }).map((key) => (
                <Kbd key={key}>{key}</Kbd>
              ))}
            </KbdGroup>
          </TooltipContent>
        </Tooltip>
      )}
      {onToggle && (
        <DetailSidebarToggleButton
          ref={focusCollapseButton}
          expand={expand}
          onToggle={handleToggle}
          icon={
            <span aria-hidden className="i-custom-vender-line-arrows-sidebar-left-arrow size-4" />
          }
        />
      )}
    </div>
  )
}
