import * as React from 'react'
import { Bar, BarChart, CartesianGrid, XAxis, YAxis, type BarRectangleItem } from 'recharts'
import { ChartContainer, ChartTooltip, type ChartConfig } from '@/components/ui/chart'
import { highlightSpecies, SPECIES_HIGHLIGHT_EVENT } from './home-motion'

type Metric = 'samples' | 'events'
type Species = {name:string; common:string; events:number; samples:number}
type Datum = Species & {code:string}
type Props = {species:Array<[string,Species]>}
const format = (value:number)=>value.toLocaleString('en-US')
const config = {
  samples:{label:'Long-read samples',color:'var(--coverage-ath)'},
  events:{label:'High-confidence IR priors',color:'var(--coverage-ir)'},
} satisfies ChartConfig
const CoverageInteraction=React.createContext<{active:string|null;select:(code:string|null)=>void}>({active:null,select:()=>{}})
function InteractionProvider({children}:{children:React.ReactNode}) {
  const [active,setActive]=React.useState<string|null>(null)
  React.useEffect(()=>{
    const clear=()=>setActive(null)
    const highlight=(event:Event)=>setActive((event as CustomEvent<{code:string|null}>).detail.code)
    window.addEventListener('plantir:route',clear)
    window.addEventListener(SPECIES_HIGHLIGHT_EVENT,highlight)
    return ()=>{window.removeEventListener('plantir:route',clear);window.removeEventListener(SPECIES_HIGHLIGHT_EVENT,highlight)}
  },[])
  // Only mark components subscribe, so highlighting does not rebuild the charts.
  return <CoverageInteraction.Provider value={{active,select:highlightSpecies}}>{children}</CoverageInteraction.Provider>
}

function useMedia(query:string) {
  const [matches,setMatches]=React.useState(()=>window.matchMedia(query).matches)
  React.useEffect(()=>{
    const media=window.matchMedia(query)
    const change=()=>setMatches(media.matches)
    media.addEventListener('change',change)
    return ()=>media.removeEventListener('change',change)
  },[query])
  return matches
}

function ScientificTick({x=0,y=0,name,compact}:{x?:number;y?:number;name:string;compact:boolean}) {
  const [genus,epithet]=name.split(' ')
  return <g transform={`translate(${x},${y})`} aria-hidden="true">
    {compact?<text transform="rotate(-48)" textAnchor="end" dy={12} className="coverage-species-tick">{genus[0]}. {epithet}</text>
      :<text textAnchor="middle" className="coverage-species-tick"><tspan x={0} dy={16}>{genus}</tspan><tspan x={0} dy={18}>{epithet}</tspan></text>}
  </g>
}

function CoverageTooltip({active,datum}:{active?:boolean;datum?:Datum}) {
  if(!active||!datum)return null
  return <div className="coverage-tooltip" role="tooltip">
    <i className="latin-name">{datum.name}</i>
    <dl><div><dt>Long-read samples</dt><dd>{format(datum.samples)}</dd></div><div><dt>IR priors</dt><dd>{format(datum.events)}</dd></div></dl>
  </div>
}

function PlotMark({shape,metric,active,select}:{shape:BarRectangleItem;metric:Metric;active:string|null;select:(code:string|null)=>void}) {
  const row=shape.payload as Datum
  const x=Number(shape.x),y=Number(shape.y),width=Number(shape.width),height=Number(shape.height)
  const middle=x+width/2
  const highlighted=active===row.code
  const colour=metric==='samples'?`var(--coverage-${row.code})`:'var(--coverage-ir)'
  return <a href={`#${metric==='samples'?'samples':'catalogue'}?species=${row.code}`}
    className="coverage-mark" data-metric={metric} data-species={row.code} data-highlighted={highlighted} data-muted={Boolean(active&&!highlighted)}
    aria-label={`${row.name}: ${format(row[metric])} ${metric==='samples'?'long-read samples':'IR priors'}. Browse ${row.common} ${metric==='samples'?'samples':'IR priors'}.`}
    tabIndex={0} onFocus={()=>select(row.code)} onBlur={()=>select(null)}
    onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();location.hash=`${metric==='samples'?'samples':'catalogue'}?species=${row.code}`}}}
    onPointerEnter={()=>select(row.code)} onPointerLeave={()=>select(null)}>
    <rect className="coverage-hit-target" x={x-8} y={Math.min(y-22,20)} width={width+16} height={Math.max(height+28,y+height-20)} rx={4}/>
    {metric==='samples'
      ?<rect className="coverage-bar" x={x} y={y} width={width} height={height} rx={3} fill={colour}/>
      :<><line className="coverage-stem" x1={middle} x2={middle} y1={y+height} y2={y} stroke={colour}/><circle className="coverage-dot" cx={middle} cy={y} r={highlighted?7:6} fill={colour}/></>}
    <text className="coverage-value" x={middle} y={y-12} textAnchor="middle">{format(row[metric])}</text>
  </a>
}

// Stable shape component identities preserve keyboard focus during linked highlights.
function SampleMark(shape:BarRectangleItem) {
  const {active,select}=React.useContext(CoverageInteraction)
  return <PlotMark shape={shape} metric="samples" active={active} select={select}/>
}
function IRMark(shape:BarRectangleItem) {
  const {active,select}=React.useContext(CoverageInteraction)
  return <PlotMark shape={shape} metric="events" active={active} select={select}/>
}

export function ResourceCoverage({species}:Props) {
  const data=React.useMemo(()=>species.map(([code,s])=>({...s,code})),[species])
  const [entered,setEntered]=React.useState(false)
  const [home,setHome]=React.useState(()=>!location.hash||location.hash.startsWith('#overview'))
  const reduced=useMedia('(prefers-reduced-motion: reduce)')
  const compact=useMedia('(max-width: 560px)')
  const root=React.useRef<HTMLElement>(null)
  const animated=React.useRef(false)
  React.useEffect(()=>{
    const observer=new IntersectionObserver(entries=>{
      if(entries.some(entry=>entry.isIntersecting)){setEntered(true);observer.disconnect()}
    },{threshold:.2})
    if(root.current)observer.observe(root.current)
    const route=(event:Event)=>{setHome((event as CustomEvent).detail.page==='overview')}
    window.addEventListener('plantir:route',route)
    return ()=>{observer.disconnect();window.removeEventListener('plantir:route',route)}
  },[])
  return <InteractionProvider><section ref={root} id="resource-coverage" className="resource-coverage" aria-labelledby="coverage-title" data-entered={entered} data-reduced-motion={reduced}>
    <div className="section-heading"><h2 id="coverage-title">Resource coverage</h2></div>
    <div className="coverage-grid">
      {(['samples','events'] as const).map(metric=><figure key={metric} className="coverage-figure" aria-labelledby={`coverage-${metric}-title`}>
        <figcaption id={`coverage-${metric}-title`}>{metric==='samples'?'Long-read samples':'High-confidence IR priors'}</figcaption>
        {home&&entered?<ChartContainer config={config} className="coverage-chart" aria-label={`${metric==='samples'?'Sample':'IR prior'} counts by species`}>
          <BarChart accessibilityLayer={false} data={data} margin={{top:32,right:10,bottom:0,left:0}} barSize={metric==='samples'?38:32}>
            <CartesianGrid vertical={false} strokeDasharray="3 5"/>
            <XAxis dataKey="code" axisLine={false} tickLine={false} interval={0} height={compact?96:54}
              tick={({x,y,payload})=><ScientificTick x={Number(x)} y={Number(y)} name={data.find(d=>d.code===payload.value)?.name||''} compact={compact}/>}/>
            <YAxis domain={[0,metric==='samples'?300:2500]} ticks={metric==='samples'?[0,100,200,300]:[0,1000,2000,2500]} axisLine={false} tickLine={false} width={56} tickFormatter={value=>format(Number(value))}/>
            <ChartTooltip cursor={false} content={({active:show,payload})=><CoverageTooltip active={show} datum={payload?.[0]?.payload as Datum|undefined}/>}/>
            <Bar dataKey={metric} isAnimationActive={!reduced&&!animated.current} animationDuration={600} animationEasing="ease-out"
              onAnimationEnd={()=>{animated.current=true}}
              shape={metric==='samples'?SampleMark:IRMark}/>
          </BarChart>
        </ChartContainer>:<div className="coverage-chart-placeholder" aria-hidden="true"/>}
        <p className="sr-only">{data.map(d=>`${d.name}: ${format(d[metric])}.`).join(' ')}</p>
      </figure>)}
    </div>
  </section></InteractionProvider>
}
