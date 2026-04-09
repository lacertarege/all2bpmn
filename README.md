# PDF to BPMN Visio

Aplicacion de escritorio para Windows, lanzada desde terminal, que:

1. recibe un `PDF` de una sola pagina con un diagrama BPMN,
2. rasteriza la pagina,
3. genera una propuesta BPMN inicial,
4. abre una UI local para revision humana obligatoria,
5. genera un `.bpmn` semantico intermedio,
6. exporta un `.vsdx` usando `Microsoft Visio` con shapes BPMN nativas.

## Estado actual

La base del proyecto ya incluye:

- arquitectura modular separando dominio, analisis, UI y exportacion a Visio;
- rasterizacion de PDF con `PyMuPDF`;
- OCR opcional con `Azure Document Intelligence`;
- propuesta BPMN opcional con un deployment multimodal de `Azure AI Foundry` via `Responses API`;
- bootstrap heuristico local con `OpenCV` cuando Azure no esta configurado;
- UI de revision con canvas editable, lista de problemas, edicion de nodos y conectores;
- bloqueo de exportacion hasta resolver todas las incidencias;
- almacenamiento local de PDF origen, imagenes y correcciones para aprendizaje futuro;
- exportacion BPMN 2.0 semantica a `.bpmn`;
- exportacion a `Visio` por `PowerShell + COM`.

La parte mas avanzada que queda por calibrar en entorno real es la deteccion semantica BPMN 2.0 casi completa y el mapeo exacto de masters BPMN de tu instalacion de Visio en español.

## Requisitos

- Windows con `Microsoft Visio O365` instalado.
- Python 3.9 o superior.
- Dependencias del proyecto.

Instalacion recomendada:

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e .
```

## Variables de entorno

El proyecto intenta cargar primero `.env` y, si no existe una variable, usa `.env.example` como fallback. Puedes tomar como base el archivo [.env.example](/mnt/e/pdf%20to%20bpmn/.env.example).

Variables principales:

- `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`
- `AZURE_DOCUMENT_INTELLIGENCE_KEY`
- `AZURE_DOCUMENT_INTELLIGENCE_MODEL` (`prebuilt-layout` por defecto)
- `AZURE_FOUNDRY_ENDPOINT`
- `AZURE_FOUNDRY_DEPLOYMENT`
- `AZURE_FOUNDRY_API_KEY`
- `AZURE_FOUNDRY_RESPONSES_URL`
- `VISIO_BPMN_TEMPLATE_HINT`
- `VISIO_BPMN_STENCIL_HINT`
- `OPEN_BPMN_JAR`

## Uso

Inspeccionar masters BPMN disponibles en Visio:

```bash
pdf2bpmn inspect-visio
```

Analizar y revisar un PDF:

```bash
pdf2bpmn review "C:\\ruta\\diagrama.pdf"
```

O indicando salida final:

```bash
pdf2bpmn review "C:\\ruta\\diagrama.pdf" --output "C:\\salida\\diagrama.vsdx"
```

En cada exportacion se generan:

- `diagrama.bpmn`: modelo BPMN 2.0 semantico;
- `diagrama.vsdx`: diagrama editable en Visio.

## Flujo de revision

En la UI:

- panel superior izquierdo: imagen del PDF origen;
- panel superior derecho: BPMN reconstruido;
- franja inferior: propiedades, problemas y listas de elementos.

La primera version permite:

- cambiar tipo BPMN;
- editar texto;
- mover nodos en el canvas;
- redimensionar por propiedades;
- corregir pools y lanes;
- cambiar origen y destino de conectores;
- marcar falsos positivos;
- resolver problemas detectados;
- exportar solo cuando no queden problemas pendientes.

## Spike de modelador embebido

La UI ahora incluye una pestana `Modelador BPMN` dentro del panel de reconstruccion:

- usa `bpmn-js` embebido en `Qt WebEngine`;
- sincroniza el `DiagramDocument` actual hacia `BPMN 2.0 XML`;
- permite editar el BPMN dentro del modelador web;
- puede reimportar el XML editado al dominio Python.

Notas del spike:

- si `QtWebEngine` no esta disponible, la pestana muestra un fallback informativo;
- el HTML del spike carga `bpmn-js` desde CDN, asi que requiere acceso de red;
- al aplicar cambios desde el modelador web, las incidencias se reinician para evitar referencias obsoletas.

## Arquitectura

Resumen en [docs/architecture.md](/mnt/e/pdf%20to%20bpmn/docs/architecture.md).

## Limitaciones actuales

- el bootstrap heuristico local es solo una base de trabajo;
- la propuesta multimodal de Azure requiere configurar variables y ajustar prompts/modelo;
- el exportador de Visio depende del stencil BPMN realmente disponible en tu instalacion y puede degradar algunos tipos especializados a masters base;
- el soporte BPMN 2.0 casi completo necesita calibracion con tus PDFs reales.
