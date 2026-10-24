/* Isolated viewer-API regression: no browser, HTTP, user records or GPU execution. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const start=source.indexOf('function residueConfidenceColorer(');
const end=source.indexOf('function selectAtom(',start);
assert.ok(start>=0&&end>start,'Viewer coloring functions must be present');
let residues=[],styles=[],surfaces=[],renderCount=0,modelAtoms=[];
const context={
  state:{style:'cartoon',color:'confidence',modelIndex:0,modelJobId:'job-x',job:{id:'job-x',result:{}},viewer:{
    removeAllSurfaces(){},setStyle(selection,style){styles.push(style);},
    addSurface(type,style){surfaces.push(style);},render(){renderCount++;},
    getModel:()=>({selectedAtoms:()=>modelAtoms})
  }},
  getResidues:()=>residues,
  getPlddt:()=>residues.map(r=>r.plddt).filter(Number.isFinite),
  encodeURIComponent,
  window:{$3Dmol:{SurfaceType:{VDW:1}}}
};
vm.createContext(context);
const helpers=source.slice(source.indexOf('function viewerModels('),source.indexOf('function renderViewerTabline('));
vm.runInContext(source.match(/function confidenceColor\(value\) \{[^\n]+\}/)[0]+'\n'+helpers+source.slice(start,end),context);
const checks=[];
function colors(style='cartoon'){
  styles=[];surfaces=[];context.state.style=style;context.applyViewerStyle();
  return style==='surface'?surfaces[0].colorfunc:styles.at(-1)[style==='stick'?'stick':'cartoon'].colorfunc;
}
residues=[{chain:'A',residueNumber:1,insertionCode:'',plddt:95}];
for(const mode of ['cartoon','stick','surface']){
  const color=colors(mode);
  assert.equal(color({chain:'A',resi:1,icode:' ',b:20}),'#65a5e8');
}
checks.push('Experimental B=20 with aligned pLDDT=95 renders high-confidence blue in cartoon, sticks and surface');
residues=[
  {chain:'A',residueNumber:10,insertionCode:'',plddt:95},
  {chain:'B',residueNumber:10,insertionCode:'',plddt:45},
  {chain:'A',residueNumber:10,insertionCode:'A',plddt:75},
  {chain:'A',residueNumber:10,insertionCode:'B',plddt:60}
];
let color=colors();
assert.equal(color({chain:'A',resi:10,icode:' ',b:1}),'#65a5e8');
assert.equal(color({chain:'B',resi:10,icode:' ',b:99}),'#e59263');
assert.equal(color({chain:'A',resi:10,icode:'A',b:1}),'#66d0d8');
assert.equal(color({chain:'A',resi:10,icode:'B',b:99}),'#e7ce68');
assert.equal(color({chain:'A',resi:10,b:99}),'#8e9ca6');
checks.push('Chain and insertion identities stay separate; omitted insertion codes in an ambiguous mmCIF group remain unknown');
residues=[
  {chain:'A',residueNumber:1,insertionCode:'',plddt:95},
  {chain:'A',residueNumber:2,insertionCode:'',plddt:null},
  {chain:'A',residueNumber:3,insertionCode:'',plddt:0},
  {chain:'A',residueNumber:4,insertionCode:'',plddt:Infinity},
  {chain:'A',residueNumber:5,insertionCode:'',plddt:101}
];
color=colors();
for(const resi of [2,4,5,6])assert.equal(color({chain:'A',resi,icode:' ',b:95}),'#8e9ca6');
assert.equal(color({chain:'A',resi:3,icode:' ',b:95}),'#e59263');
checks.push('Missing, invalid or unmapped confidence is neutral gray; a genuine zero remains a low-confidence value');
context.state.color='chain';color=colors();
assert.equal(color({chain:'A',resi:2,b:95}),'#9edac6');
context.state.color='spectrum';colors();assert.equal(styles.at(-1).cartoon.color,'spectrum');
checks.push('Explicit chain and spectrum coloring retain their previous semantics');
assert.equal(renderCount,7);
context.state.job={id:'job-x',result:{rankedModels:[{},{}]}};
context.state.modelIndex=1;
modelAtoms=[{chain:'A',b:92},{chain:'A',b:35},{chain:'A',b:64},{chain:'A',b:0}];
const bColorer=context.bFactorConfidenceColorer();
assert.equal(typeof bColorer,'function');
assert.equal(bColorer({chain:'A',b:92}),'#65a5e8');
assert.equal(bColorer({chain:'A',b:35}),'#e59263');
assert.equal(bColorer({chain:'A',b:64}),'#e7ce68');
assert.equal(bColorer({chain:'A',b:0}),'#8e9ca6');
checks.push('A non-primary ranked model colors per-residue pLDDT from the structure file B-factor column in the shared four-band palette');
modelAtoms=[{chain:'A',b:0},{chain:'A',b:0},{chain:'A',b:1e9}];
assert.equal(context.bFactorConfidenceColorer(),null);
residues=[{chain:'A',residueNumber:1,insertionCode:'',plddt:95}];
styles=[];context.state.color='confidence';context.applyViewerStyle();
assert.equal(styles.at(-1).cartoon.colorfunc({chain:'A',b:92}),'#9edac6');
checks.push('Files whose B-factor column carries no usable pLDDT fall back to chain coloring instead of inventing confidence');
context.state.modelIndex=0;modelAtoms=[];
console.log(JSON.stringify({status:'passed',count:checks.length,checks,scope:'Production coloring functions evaluated against a minimal 3Dmol viewer API fixture; no WebGL/native model claim.'},null,2));
