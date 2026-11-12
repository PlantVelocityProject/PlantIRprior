import { useLayoutEffect, useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue, type SelectActions } from '@/components/ui/select'

function FilterSelect({ control }: { control: HTMLSelectElement }) {
  const actions = useRef<SelectActions>(null)
  const triggerId = `${control.id}-trigger`
  const items = Array.from(control.options, option => ({
    value: option.value,
    label: option.text,
    disabled: option.disabled,
  }))

  useLayoutEffect(() => {
    const label = control.labels?.[0]
    const close = () => actions.current?.close()
    control.hidden = true
    if (label) label.htmlFor = triggerId
    window.addEventListener('plantir:route', close)
    return () => {
      window.removeEventListener('plantir:route', close)
      control.hidden = false
      if (label) label.htmlFor = control.id
    }
  }, [control, triggerId])

  const formatLabel = (value: string, label: string) =>
    control.dataset.filterSelect === 'species' && value !== 'all' ? <i className="latin-name">{label}</i> : label

  return (
    <Select
      actionsRef={actions}
      items={items.map(item => ({value: item.value, label: formatLabel(item.value, item.label)}))}
      value={control.value}
      disabled={control.disabled}
      modal={false}
      onValueChange={value => {
        if (value === null || control.disabled || value === control.value) return
        control.value = value
        control.dispatchEvent(new Event('change', {bubbles: true}))
      }}
    >
      <SelectTrigger id={triggerId}><SelectValue className="filter-select-value" /></SelectTrigger>
      <SelectContent>
        <SelectGroup>
          {items.map(({value, label, disabled}) => (
            <SelectItem key={value} value={value} label={label} disabled={disabled}>
              {formatLabel(value, label)}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}

export function installFilterSelects() {
  // The native controls own filter values and remain available before the UI bundle loads.
  const mounted = new Map<HTMLSelectElement, {render: () => void; dispose: () => void}>()
  const sync = () => {
    for (const [control, instance] of mounted) {
      if (control.isConnected) continue
      instance.dispose()
      mounted.delete(control)
    }
    for (const control of document.querySelectorAll<HTMLSelectElement>('select[data-filter-select]')) {
      let instance = mounted.get(control)
      if (!instance) {
        const host = document.createElement('div')
        host.className = 'plantir-ui filter-select-host'
        control.after(host)
        const root = createRoot(host)
        const render = () => root.render(<FilterSelect control={control} />)
        control.addEventListener('change', render)
        instance = {render, dispose: () => {
          control.removeEventListener('change', render)
          root.unmount()
          host.remove()
        }}
        mounted.set(control, instance)
      }
      instance.render()
    }
  }
  window.addEventListener('plantir:filters', sync)
  sync()
}
