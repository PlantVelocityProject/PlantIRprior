import * as React from 'react'
import {figureModules, figureViewport, type FigureModule} from './methods-figure'
import {countBounds, methodConnections, methodRelations} from './methods-motion'
import type {MethodConnection, MethodRelation} from './methods-motion'

type Geometry = {d: string; transform?: string}
type Connection = {paths: Geometry[]; heads: Geometry[]; reverse?:boolean}

function readConnections(artwork:string, surfaceId:string, definitions:Record<string,MethodConnection>): Record<string, Connection> {
  const document = new DOMParser().parseFromString(artwork, 'image/svg+xml')
  const surface = document.getElementById(surfaceId)
  if (!surface) return {}
  const geometry = (index: number): Geometry | null => {
    const node = surface.querySelector(`[data-method-object="${index}"]`) || surface.children[index]
    if (node?.localName !== 'path' || !node.getAttribute('d')) return null
    return {d: node.getAttribute('d')!, transform: node.getAttribute('transform') || undefined}
  }
  return Object.fromEntries(Object.entries(definitions).flatMap(([id, connection]) => {
    const paths = connection.paths.map(geometry)
    const heads = (connection.heads || (connection.head===undefined?[]:[connection.head])).map(geometry)
    return heads.every((head):head is Geometry=>head!==null) && paths.every((path): path is Geometry => path !== null)
      ? [[id, {paths, heads, reverse:connection.reverse}]] : []
  }))
}

type MotionProps={
 active:string|null; artwork:string; modules?:FigureModule[]; viewport?:typeof figureViewport
 relations?:Record<string,MethodRelation>; connections?:Record<string,MethodConnection>
 surfaceId?:string; countRows?:Record<string,[number,number,number,number]>
}

export function MethodsMotion({active, artwork, modules=figureModules, viewport=figureViewport,
  relations=methodRelations, connections:definitions=methodConnections, surfaceId='methods-surface1', countRows=countBounds}:MotionProps) {
  const connections = React.useMemo(() => readConnections(artwork,surfaceId,definitions), [artwork,surfaceId,definitions])
  const relation = active ? relations[active] : undefined
  const view = viewport
  return <svg className="methods-motion" viewBox={`${view.x} ${view.y} ${view.width} ${view.height}`} aria-hidden="true" focusable="false"
    data-active={active || undefined} data-excluded={relation?.excluded || undefined}
    style={{'--method-accent': relation?.color || 'var(--pine)'} as React.CSSProperties}>
    {modules.map(module => {
      const selected = module.id === active
      const related = Boolean(relation?.related.includes(module.id))
      const [x, y, width, height] = module.bounds
      return <rect key={module.id} className="methods-relation-outline" data-relation-module={module.id}
        data-state={selected ? 'active' : related ? 'related' : 'idle'}
        x={x - 1.2} y={y - 1.2} width={width + 2.4} height={height + 2.4} rx={5}/>
    })}
    {Object.entries(countRows).map(([count, [x, y, width, height]]) => <rect key={count}
      className="methods-count-highlight" data-count={count} data-highlighted={Boolean(relation?.counts.includes(count as keyof typeof countBounds))}
      x={x} y={y} width={width} height={height} rx={2}/>)}
    {/* Replacing this one finite group cancels old traces on rapid selection. */}
    {relation && <g key={active} className="methods-traces">
      {relation.connections.map(id => {
        const connection = connections[id]
        if (!connection) return null
        return <g key={id} data-method-connection={id} data-reverse={connection.reverse || undefined}>
          {connection.paths.map((path, index) => <path key={index} {...path} className="methods-trace" pathLength={1}
            style={{animationDuration: `${400 / connection.paths.length}ms`, animationDelay: `${index * 400 / connection.paths.length}ms`}}/>)}
          {connection.heads.map((head,index)=><path key={index} {...head} className="methods-trace-head"/>)}
        </g>
      })}
    </g>}
  </svg>
}
