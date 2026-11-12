import * as React from 'react'
import {Tooltip} from '@base-ui/react/tooltip'
import type {FigureModule, figureViewport} from './methods-figure'
import {MethodsMotion} from './methods-motion.tsx'

type WorkflowProps={
 id:string; label:string; artwork:string; modules:FigureModule[]; viewport:typeof figureViewport
 active:string|null; setActive:React.Dispatch<React.SetStateAction<string|null>>
 motion?:Pick<React.ComponentProps<typeof MethodsMotion>,'relations'|'connections'|'surfaceId'|'countRows'>
}

/** Both diagrams share selection and the same stationary tooltip/motion behavior. */
export function MethodsWorkflow({id,label,artwork,modules,viewport:view,active,setActive,motion}:WorkflowProps){
 const artworkRef=React.useRef<HTMLDivElement>(null)
 const explanationRef=React.useRef<HTMLDivElement>(null)
 const pressedOpen=React.useRef(false)
 const openId=modules.some(module=>module.id===active)?active:null

 React.useEffect(()=>{
  const dismiss=()=>setActive(current=>modules.some(module=>module.id===current)?null:current)
  const route=(event:Event)=>{if((event as CustomEvent).detail.page!=='methods')dismiss()}
  const visibility=()=>{if(document.hidden)dismiss()}
  window.addEventListener('plantir:route',route)
  window.addEventListener('resize',dismiss)
  document.addEventListener('visibilitychange',visibility)
  const observer=new IntersectionObserver(entries=>{
   if(entries.some(entry=>!entry.isIntersecting))dismiss()
  })
  if(artworkRef.current)observer.observe(artworkRef.current)
  return ()=>{
   window.removeEventListener('plantir:route',route)
   window.removeEventListener('resize',dismiss)
   document.removeEventListener('visibilitychange',visibility)
   observer.disconnect()
  }
 },[modules,setActive])

 return <figure id={id} aria-label={label}>
  <div className="methods-viewport">
   <div className="methods-canvas">
    <div className="methods-artwork" ref={artworkRef} dangerouslySetInnerHTML={{__html:artwork}}/>
    <MethodsMotion {...motion} active={openId} artwork={artwork} modules={modules} viewport={view}/>
    {modules.map(module=>{
     const [x,y,w,h]=module.bounds
     return <Tooltip.Root key={module.id} open={openId===module.id}
      onOpenChange={open=>setActive(current=>open?module.id:current===module.id?null:current)}>
      <Tooltip.Trigger
       id={`method-module-${module.id}`} className="methods-module-trigger" data-module={module.id}
       aria-label={module.title} aria-pressed={openId===module.id}
       aria-describedby={openId===module.id?`method-tooltip-${module.id}`:undefined}
       delay={200} closeDelay={120} closeOnClick={false}
       style={{left:`${(x-view.x)/view.width*100}%`,top:`${(y-view.y)/view.height*100}%`,width:`${w/view.width*100}%`,height:`${h/view.height*100}%`}}
       onPointerDown={()=>{pressedOpen.current=openId===module.id}}
       onKeyDown={event=>{if(event.key==='Enter'||event.key===' ')pressedOpen.current=openId===module.id}}
       onClick={()=>setActive(pressedOpen.current?null:module.id)}
      />
      <Tooltip.Portal>
       <Tooltip.Positioner className="methods-tooltip-positioner" anchor={explanationRef} side="bottom" align="start"
        sideOffset={({anchor})=>-anchor.height} positionMethod="fixed" collisionAvoidance={{side:'none',align:'none',fallbackAxisSide:'none'}}>
        <Tooltip.Popup id={`method-tooltip-${module.id}`} role="tooltip" className="methods-tooltip">
         <strong>{module.title}</strong><p>{module.description}</p>
        </Tooltip.Popup>
       </Tooltip.Positioner>
      </Tooltip.Portal>
     </Tooltip.Root>
    })}
   </div>
   <div className="methods-explanation-space" ref={explanationRef}/>
  </div>
 </figure>
}
