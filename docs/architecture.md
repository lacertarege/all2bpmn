# Arquitectura propuesta

## Decision de stack

Se eligio `Python` como lenguaje principal porque el cuello de botella del problema no es generar el `.vsdx`, sino interpretar diagramas BPMN desde escaneos y fotos de baja calidad. Python ofrece mejor ecosistema para:

- rasterizacion y extraccion desde PDF,
- preprocesamiento visual,
- OCR,
- integracion con modelos multimodales,
- prototipado rapido del pipeline de aprendizaje.

La UI se implementa con `PySide6`, lo que permite una aplicacion de escritorio real en Windows sin dividir demasiado pronto el sistema entre varios lenguajes.

## Fronteras del sistema

### 1. Dominio BPMN

`src/pdf_to_bpmn/domain.py`

Modela:

- nodos BPMN,
- conectores,
- problemas de revision,
- documento reconstruido.

### 2. Pipeline de analisis

`src/pdf_to_bpmn/services/rasterizer.py`
`src/pdf_to_bpmn/services/analysis.py`
`src/pdf_to_bpmn/services/azure_document.py`

Orden:

1. rasterizar PDF de una sola pagina;
2. ejecutar OCR si Azure Document Intelligence esta configurado;
3. pedir propuesta semantica a Azure AI Foundry si existe deployment multimodal;
4. si no existe, generar bootstrap heuristico local con OpenCV;
5. fusionar OCR, geometria y problemas de confianza.

## 3. UI de revision

`src/pdf_to_bpmn/ui/main_window.py`
`src/pdf_to_bpmn/ui/scene.py`

Objetivos:

- mostrar origen y reconstruccion lado a lado;
- editar elementos detectados;
- bloquear exportacion mientras haya ambiguedades sin resolver;
- guardar estado revisado para aprendizaje posterior.

## 4. Exportacion a Visio

`src/pdf_to_bpmn/services/bpmn_semantic.py`
`src/pdf_to_bpmn/services/visio.py`

La salida ya no depende solo de dibujar shapes en Visio. Ahora el pipeline hace:

1. construir un `.bpmn` semantico con elementos BPMN 2.0 y `BPMNDI`;
2. exportar un `.vsdx` editable en Visio.

Se usa `PowerShell + COM` en el segundo paso porque:

- el formato objetivo es `VSDX`,
- el usuario ya tiene `Visio O365` instalado,
- se necesitan shapes BPMN nativas de Visio y no figuras dibujadas manualmente.

La implementacion actual:

- materializa un `collaboration` y uno o mas `process`;
- modela `participants`, `laneSet`, `lane`, `flow nodes` y `flows`;
- serializa `sequenceFlow` y `messageFlow` con layout `BPMNDI`;
- preserva jerarquia `pool -> lane -> flow node` tanto como permite la geometria detectada;
- inspecciona templates/stencils BPMN disponibles,
- busca masters por alias en español e ingles,
- posiciona shapes preservando layout relativo,
- conecta flujos por `GlueToPos`,
- guarda el `.vsdx`.

### Open-BPMN como adaptacion util

De `open-bpmn` lo aprovechable para este proyecto no es el editor GLSP, sino la idea de trabajar sobre un modelo BPMN real antes de dibujar nada. Por eso esta version introduce un `.bpmn` intermedio como artefacto canonico del proceso.

Se deja ademas un hook opcional para normalizacion con Open-BPMN si mas adelante empaquetas su `metamodel` como `jar` ejecutable y lo apuntas por `OPEN_BPMN_JAR`.

## Tecnologias mas avanzadas recomendadas

### OCR y layout

- `Azure Document Intelligence v4` para `prebuilt-read` y `layout`.
- Valor: OCR robusto en documentos escaneados y mejoras recientes en OCR y deteccion de figuras.

### Comprension semantica del diagrama

- deployment multimodal en `Azure AI Foundry` para proponer nodos, conectores, tipos BPMN y problemas de baja confianza.
- Valor: mejor bootstrap semantico que reglas puramente geometricas.

### Recuperacion geometrica local

- `OpenCV` para deskew, thresholding, contornos, lineas y conectores.
- Valor: fallback local y complemento de la propuesta semantica.

### PDF rasterization

- `PyMuPDF`.
- Valor: rapido y confiable para pasar del PDF de una pagina a una imagen de trabajo.

### Exportacion final

- `Microsoft Visio COM automation`.
- Valor: permite usar masters BPMN nativos y generar `.vsdx` editable en la propia herramienta del usuario.

## Ruta de evolucion

### V1 actual

- bootstrap heuristico + OCR/Foundry opcional,
- revision humana obligatoria,
- exportacion a `.vsdx`.

### V1.1

- mejorar snapping de conectores,
- inventario robusto de masters BPMN en Visio español,
- mas tipos BPMN especificos en bootstrap,
- validacion y normalizacion automatica del `.bpmn` intermedio con Open-BPMN.

### V2

- dataset local de correcciones verificadas,
- entrenamiento incremental y active learning,
- mejor priorizacion de ambiguedades.
