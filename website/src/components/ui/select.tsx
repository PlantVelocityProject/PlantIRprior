import { Select as SelectPrimitive } from "@base-ui/react/select"
import { Check, ChevronDown } from "lucide-react"
import { cn } from "@/lib/utils"

const Select = SelectPrimitive.Root
const SelectValue = SelectPrimitive.Value
const SelectGroup = SelectPrimitive.Group
type SelectActions = SelectPrimitive.Root.Actions

function SelectTrigger({ children, className, ...props }: SelectPrimitive.Trigger.Props) {
  return (
    <SelectPrimitive.Trigger data-slot="select-trigger" className={cn("filter-select-trigger", className)} {...props}>
      {children}
      <ChevronDown className="menu-chevron" aria-hidden="true" />
    </SelectPrimitive.Trigger>
  )
}

function SelectContent({ children, className, ...props }: SelectPrimitive.Popup.Props) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Positioner
        className="isolate z-50"
        align="start"
        sideOffset={8}
        collisionPadding={10}
        alignItemWithTrigger={false}
      >
        <SelectPrimitive.Popup data-slot="select-content" className={cn("plantir-ui menu-surface filter-select-content", className)} {...props}>
          <SelectPrimitive.List data-slot="select-list" className="filter-select-list">
            {children}
          </SelectPrimitive.List>
        </SelectPrimitive.Popup>
      </SelectPrimitive.Positioner>
    </SelectPrimitive.Portal>
  )
}

function SelectItem({ children, className, ...props }: SelectPrimitive.Item.Props) {
  return (
    <SelectPrimitive.Item data-slot="select-item" className={cn("menu-item filter-select-item", className)} {...props}>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
      <SelectPrimitive.ItemIndicator data-slot="select-item-indicator" className="filter-select-check">
        <Check size={16} aria-hidden="true" />
      </SelectPrimitive.ItemIndicator>
    </SelectPrimitive.Item>
  )
}

export { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue, type SelectActions }
