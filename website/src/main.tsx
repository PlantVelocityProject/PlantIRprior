import * as React from 'react'
import { createRoot } from 'react-dom/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { NavigationMenu, NavigationMenuContent, NavigationMenuItem, NavigationMenuLink, NavigationMenuList, NavigationMenuTrigger } from '@/components/ui/navigation-menu'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import { ArrowUpRight, BookOpen, CodeXml, Download, FileText, ListFilter, Menu } from 'lucide-react'
import { ResourceCoverage } from './resource-coverage'
import { Methods } from './methods'
import { ReleaseVersionMenu } from './release-version-menu'
import { installFilterSelects } from './filter-selects'
import { installHomeMotion } from './home-motion'
import './ui.css'

type Species = {name:string; common:string; events:number; samples:number; assembly:string; annotation:string; art?:string}
declare global {
  interface Window { PlantIR: {species:Record<string,Species>; speciesOrder:string[]; downloads:Record<string,string>; resourceHref:(href:string)=>string} }
}

const species = window.PlantIR.speciesOrder.map<[string,Species]>(code=>[code,window.PlantIR.species[code]])
const format = (n:number)=>n.toLocaleString('en-US')
const assets:Record<string,string> = {ath:'arabidopsis',gmx:'soybean',zma:'maize',sly:'tomato',osa:'rice'}
const destinations = [
  {title:'All downloads',description:'IR events, PlantVelo priors and data bundles',href:'#resources',icon:Download,keywords:'csv zip resources data download'},
  {title:'Genomes & annotations',description:'Assemblies and gene model versions',href:'#references',icon:BookOpen,keywords:'references genome assembly annotation'},
  {title:'Source samples',description:'Included long-read sequencing records',href:'#samples',icon:ListFilter,keywords:'sample libraries metadata accession'},
  {title:'Releases',description:'Version history, provenance and downloads',href:'#releases',icon:FileText,keywords:'provenance checksum source update'},
]
function SpeciesCards() {
  React.useEffect(installHomeMotion, [])
  return <>{species.map(([code,s])=>{
    const download=window.PlantIR.downloads[code]
    return <Card key={code} className="botanical-card gap-0" data-species={code}>
      <CardContent className="species-art p-0"><img src={`assets/${s.art||assets[code]}.webp`} alt="" width="170" height="180"/></CardContent>
      <CardHeader className="card-species-header"><CardTitle>{s.common}</CardTitle><CardDescription><i className="latin-name">{s.name}</i></CardDescription></CardHeader>
      <CardContent className="card-species-counts"><p><strong>{format(s.events)}</strong> IR priors</p><p>{s.samples} long-read samples</p></CardContent>
      <CardFooter className="card-species-actions gap-1">
        <Button variant="secondary" nativeButton={false} render={<a href={`#catalogue?species=${code}`} data-species={code}/>} className="flex-1">Browse<ArrowUpRight data-icon="inline-end"/></Button>
        <Button variant="ghost" nativeButton={false} render={<a href={download} download={download!=='#releases'}/>} aria-label={`Download ${s.name} IR events CSV`}><Download data-icon="inline-start"/><span className="download-label">CSV</span></Button>
      </CardFooter>
    </Card>
  })}</>
}

function Header() {
  const [sheetOpen,setSheetOpen]=React.useState(false)
  const [navigation,setNavigation]=React.useState<string|null>(null)
  const [current,setCurrent]=React.useState(location.hash.split('?')[0].slice(1)||'overview')
  React.useEffect(()=>{
    const route=(event:Event)=>{setCurrent((event as CustomEvent).detail.page);setSheetOpen(false);setNavigation(null)}
    window.addEventListener('plantir:route',route)
    return ()=>{window.removeEventListener('plantir:route',route)}
  },[])
  const resourcesActive=['resources','references','samples'].includes(current)
  return <>
    <div className="desktop-navigation">
      <NavigationMenu value={navigation} onValueChange={setNavigation} aria-label="Main navigation" align="end">
        <NavigationMenuList>
          <NavigationMenuItem><NavigationMenuLink href="#overview" active={current==='overview'}>Home</NavigationMenuLink></NavigationMenuItem>
          <NavigationMenuItem><NavigationMenuLink href="#catalogue" active={current==='catalogue'}>Browse</NavigationMenuLink></NavigationMenuItem>
          <NavigationMenuItem><NavigationMenuLink href="#releases" active={['releases','release'].includes(current)}>Releases</NavigationMenuLink></NavigationMenuItem>
          <NavigationMenuItem value="resources"><NavigationMenuTrigger data-active={resourcesActive||undefined}>Resources</NavigationMenuTrigger><NavigationMenuContent className="plantir-ui resource-menu"><div className="resource-menu-grid">{destinations.map(p=><NavigationMenuLink key={p.href} className="menu-item" href={window.PlantIR.resourceHref(p.href)} onClick={()=>setNavigation(null)}><p.icon/><span><strong>{p.title}</strong><small>{p.description}</small></span></NavigationMenuLink>)}</div></NavigationMenuContent></NavigationMenuItem>
          <NavigationMenuItem><NavigationMenuLink href="#methods" active={current==='methods'}>Methods</NavigationMenuLink></NavigationMenuItem>
        </NavigationMenuList>
      </NavigationMenu>
    </div>
    <Button variant="ghost" nativeButton={false} render={<a href="https://github.com/PlantVelocityProject" target="_blank" rel="noopener"/>} className="header-github" aria-label="PlantVelocity on GitHub"><CodeXml data-icon="inline-start"/><span>GitHub</span></Button>
    <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
      <SheetTrigger render={<Button variant="ghost" size="icon" className="mobile-navigation-trigger" aria-label="Open navigation"/>}><Menu/></SheetTrigger>
      <SheetContent className="plantir-ui mobile-navigation-sheet">
        <SheetHeader><SheetTitle>Navigation</SheetTitle><SheetDescription>PlantIR Prior resources</SheetDescription></SheetHeader>
        <nav aria-label="Mobile navigation" className="sheet-navigation">
          <a href="#overview">Home</a><a href="#catalogue">Browse IR priors</a><a href="#releases">Releases</a><a href="#methods">Methods</a>
          <span className="sheet-group-title">Resources</span>
          {destinations.map(p=><a key={p.href} href={window.PlantIR.resourceHref(p.href)}><p.icon/>{p.title}</a>)}
        </nav>
      </SheetContent>
    </Sheet>
  </>
}

const header=document.getElementById('header-ui')
const cards=document.getElementById('species-list')
const coverage=document.getElementById('resource-coverage-ui')
if(header)createRoot(header).render(<Header/>)
if(cards)createRoot(cards).render(<SpeciesCards/>)
if(coverage)createRoot(coverage).render(<ResourceCoverage species={species}/>)
installFilterSelects()

const methods=document.getElementById('methods-ui')
if(methods)createRoot(methods).render(<Methods/>)

const releaseMenu=document.getElementById('release-version-menu-ui')
if(releaseMenu){
  const groups=window.PlantIRReleases.menuGroups()
  if(groups.length)createRoot(releaseMenu).render(<ReleaseVersionMenu groups={groups}/>)
  else releaseMenu.hidden=true
}
