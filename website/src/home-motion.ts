/** Shared species attention connects the botanical specimens to their evidence. */
export const SPECIES_HIGHLIGHT_EVENT = 'plantir:species-highlight'

export function highlightSpecies(code: string | null) {
  // Pointer exit must not erase the species still held by keyboard focus.
  if (!code && document.activeElement?.matches(':focus-visible')) {
    code = document.activeElement.closest<HTMLElement>('.botanical-card, .coverage-mark')?.dataset.species || null
  }
  window.dispatchEvent(new CustomEvent(SPECIES_HIGHLIGHT_EVENT, {detail: {code}}))
}

export function installHomeMotion() {
  const home = document.getElementById('page-overview')
  const list = document.getElementById('species-list')
  if (!home || !list) return

  const cards = [...list.querySelectorAll<HTMLElement>('.botanical-card')]
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
  const seen = new WeakSet<HTMLImageElement>()
  const playing = new Map<HTMLImageElement, Animation>()
  let disposed = false

  const cancel = () => {
    playing.forEach(animation => animation.cancel())
    playing.clear()
  }
  const grow = async (image: HTMLImageElement, delay: number) => {
    if (seen.has(image)) return
    seen.add(image)
    // Never conceal content while images decode, or when motion is unavailable.
    try { await image.decode() } catch { return }
    const rect = image.getBoundingClientRect()
    if (disposed || home.hidden || document.hidden || reduced.matches || rect.bottom <= 0 || rect.top >= innerHeight || typeof image.animate !== 'function') return
    const animation = image.animate([
      {clipPath: 'inset(58% 0 0)', transform: 'translateY(7px) scale(.98, .94)', opacity: .65},
      {clipPath: 'inset(0% 0 0)', transform: 'none', opacity: 1},
    ], {duration: 620, delay, easing: 'cubic-bezier(0.16, 1, 0.3, 1)', fill: 'backwards'})
    playing.set(image, animation)
    animation.finished.then(() => playing.delete(image), () => playing.delete(image))
  }
  const observer = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
    let order = 0
    for (const entry of entries) {
      const image = entry.target as HTMLImageElement
      if (entry.isIntersecting && !home.hidden) void grow(image, Math.min(order++ * 35, 140))
      else { playing.get(image)?.cancel(); playing.delete(image) }
    }
  }, {threshold: .2}) : null
  cards.forEach(card => {
    const image = card.querySelector<HTMLImageElement>('.species-art img')
    if (image) observer?.observe(image)
  })

  const cardFor = (target: EventTarget | null) => target instanceof Element ? target.closest<HTMLElement>('.botanical-card') : null
  const focusCode = () => document.activeElement?.closest<HTMLElement>('.botanical-card, .coverage-mark')?.dataset.species || null
  const pointerOver = (event: PointerEvent) => {
    if (event.pointerType === 'touch') return
    const card = cardFor(event.target)
    if (card && card !== cardFor(event.relatedTarget)) highlightSpecies(card.dataset.species || null)
  }
  const pointerOut = (event: PointerEvent) => {
    if (event.pointerType === 'touch') return
    const card = cardFor(event.target)
    if (card && card !== cardFor(event.relatedTarget)) highlightSpecies(focusCode())
  }
  const focusIn = (event: FocusEvent) => highlightSpecies(cardFor(event.target)?.dataset.species || null)
  const focusOut = () => queueMicrotask(() => { if (!disposed) highlightSpecies(focusCode()) })
  const highlight = (event: Event) => {
    const {code} = (event as CustomEvent<{code: string | null}>).detail
    cards.forEach(card => { card.dataset.highlighted = String(card.dataset.species === code) })
  }
  const route = () => { if (home.hidden) { cancel(); highlightSpecies(null) } }
  const visibility = () => { if (document.hidden) cancel() }
  const preference = () => { if (reduced.matches) cancel() }

  list.addEventListener('pointerover', pointerOver)
  list.addEventListener('pointerout', pointerOut)
  list.addEventListener('focusin', focusIn)
  list.addEventListener('focusout', focusOut)
  window.addEventListener(SPECIES_HIGHLIGHT_EVENT, highlight)
  window.addEventListener('plantir:route', route)
  document.addEventListener('visibilitychange', visibility)
  reduced.addEventListener('change', preference)
  return () => {
    disposed = true
    cancel()
    observer?.disconnect()
    list.removeEventListener('pointerover', pointerOver)
    list.removeEventListener('pointerout', pointerOut)
    list.removeEventListener('focusin', focusIn)
    list.removeEventListener('focusout', focusOut)
    window.removeEventListener(SPECIES_HIGHLIGHT_EVENT, highlight)
    window.removeEventListener('plantir:route', route)
    document.removeEventListener('visibilitychange', visibility)
    reduced.removeEventListener('change', preference)
  }
}
