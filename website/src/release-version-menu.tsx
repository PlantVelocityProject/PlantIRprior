import { useEffect, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuLinkItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

type ReleaseGroup = {
  species: string
  versions: {
    href: string
    generation: number
    current: boolean
    createdAt: string
    dateLabel: string
  }[]
}

declare global {
  interface Window {
    PlantIRReleases: {menuGroups: () => ReleaseGroup[]}
  }
}

export function ReleaseVersionMenu({groups}: {groups: ReleaseGroup[]}) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const close = () => setOpen(false)
    window.addEventListener('plantir:route', close)
    return () => window.removeEventListener('plantir:route', close)
  }, [])

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger render={<Button variant="outline" className="min-h-11 text-primary" />}>
        Choose release <ChevronDown className="menu-chevron" data-icon="inline-end" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={8} className="plantir-ui w-80 max-w-[calc(100vw-2rem)] p-2">
        {groups.map(group => (
          <DropdownMenuGroup key={group.species} className="not-first:mt-1">
            <DropdownMenuLabel>{group.species}</DropdownMenuLabel>
            {group.versions.map(version => (
              <DropdownMenuLinkItem
                key={version.href}
                href={version.href}
                label={`${group.species} Release ${version.generation}`}
              >
                <span className="font-medium">Release {version.generation}</span>
                <span className={version.current ? 'release-status' : 'release-status is-archived'}>
                  {version.current ? 'Current' : 'Archived'}
                </span>
                <time dateTime={version.createdAt} className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">
                  {version.dateLabel}
                </time>
              </DropdownMenuLinkItem>
            ))}
          </DropdownMenuGroup>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuLinkItem href="#release-history" className="font-medium text-primary">
            Explore release history
          </DropdownMenuLinkItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
