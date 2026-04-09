# Exploracion de integracion con Bizagi

Fecha de revision: 2026-04-07

## Resumen ejecutivo

Para la aplicacion actual, la via realista de integracion con Bizagi no es incrustar Bizagi Modeler dentro de nuestra UI, sino interoperar por `BPMN 2.0` y, en segundo plano, considerar Bizagi como:

- destino de importacion en `Bizagi Modeler`;
- origen para automatizacion posterior en `Bizagi Studio`;
- portal web externo para colaboracion y consulta.

No encontre evidencia oficial de un SDK publico de componentes visuales de Bizagi Modeler que podamos embeber dentro de una aplicacion propia de escritorio. La documentacion oficial vigente describe:

- `Bizagi Modeler` como aplicacion de escritorio;
- `Modeler Services` como servicios cloud accesibles via navegador;
- importacion y exportacion por formatos interoperables como `BPMN`, `XPDL` y `Visio`.

Por eso, si el objetivo es tener un modelador BPMN embebido dentro de nuestra aplicacion, la recomendacion tecnica es:

1. mantener `Bizagi` como sistema externo de consumo del `.bpmn`;
2. usar un editor embebible propio para la experiencia visual dentro de la app;
3. elegir `bpmn-js` como primera opcion para ese editor embebible.

## Lo que si soporta Bizagi y nos sirve

### 1. Importar BPMN en Bizagi Modeler

Bizagi documenta que puede importar diagramas `BPMN 2.0 XML` creados en otras herramientas y editarlos como si hubieran sido creados dentro de Modeler.

Implicacion para nosotros:

- nuestro exportador `.bpmn` ya es el punto correcto de integracion;
- la mejora mas rentable no es cambiar la UI, sino endurecer la compatibilidad BPMN para Bizagi.

### 2. Llevar BPMN hacia Bizagi Studio

Bizagi tambien documenta la importacion de un archivo `BPMN` en `Bizagi Studio` para convertirlo en un proceso listo para automatizacion.

Implicacion para nosotros:

- la app puede posicionarse como herramienta de reconstruccion y limpieza de procesos;
- Bizagi Studio queda como siguiente etapa del flujo.

### 3. Colaboracion web, pero no como componente embebible documentado

La documentacion de `Modeler Services` y `Process Library` habla de acceso por navegador y repositorio cloud, no de un widget o componente reutilizable para integracion OEM.

Implicacion para nosotros:

- podemos enlazar o abrir Bizagi externamente;
- no deberiamos planificar una incrustacion nativa de Bizagi sin un acuerdo directo con Bizagi y documentacion privada.

## Evaluacion de opciones

### Opcion A. Integracion por interoperabilidad BPMN

Descripcion:

- seguir usando nuestra UI local para revisar el diagrama;
- exportar `.bpmn`;
- abrir ese archivo en `Bizagi Modeler` o importarlo en `Bizagi Studio`.

Ventajas:

- menor riesgo tecnico y legal;
- aprovecha lo que el producto Bizagi soporta oficialmente;
- encaja con el estado actual del proyecto;
- no exige reescribir la UI.

Desventajas:

- la edicion final en Bizagi sigue siendo un paso separado;
- no existe una experiencia unica dentro de nuestra ventana.

Veredicto:

- es la mejor opcion inmediata.

### Opcion B. Lanzar Bizagi Modeler como aplicacion externa desde nuestra app

Descripcion:

- agregar un boton `Abrir en Bizagi`;
- guardar el `.bpmn`;
- lanzar Bizagi Modeler como proceso externo, dejando la importacion final al usuario.

Ventajas:

- flujo mas directo;
- esfuerzo bajo.

Desventajas:

- depende de instalacion local y rutas de Bizagi;
- la importacion automatica puede no estar soportada por linea de comandos publica;
- seguimos fuera de nuestra ventana.

Veredicto:

- viable como mejora UX, no como integracion embebida.

### Opcion C. Intentar incrustar Bizagi Desktop en una ventana Qt

Descripcion:

- lanzar la app de escritorio y tratar de hostear su ventana dentro de `PySide6` mediante `HWND`.

Ventajas:

- aparenta una integracion completa.

Desventajas:

- altamente fragil;
- no soportado oficialmente;
- sensible a cambios de version de Bizagi y Windows;
- complica foco, menus, DPI, dialogos modales y actualizaciones;
- riesgo de licencia y soporte.

Veredicto:

- no recomendado.

### Opcion D. Incrustar una pagina o portal Bizagi en `QtWebEngine`

Descripcion:

- cargar `Process Library` o algun portal Bizagi en una vista web dentro de la app.

Ventajas:

- puede servir para consulta o navegacion.

Desventajas:

- no equivale a tener el modelador embebido;
- pueden existir restricciones de autenticacion, CSP, iframes y sesiones;
- la experiencia depende totalmente de lo que Bizagi exponga en web.

Veredicto:

- util solo para navegacion o consulta, no como editor principal.

### Opcion E. Editor BPMN embebido propio y Bizagi como destino

Descripcion:

- mantener el pipeline actual en Python;
- incrustar un editor/modelador BPMN web dentro de la app;
- sincronizarlo con nuestro `.bpmn`;
- usar Bizagi como herramienta downstream.

Ventajas:

- control total de la experiencia;
- integracion real dentro de nuestra aplicacion;
- menor dependencia de restricciones del producto Bizagi.

Desventajas:

- hay que integrar un motor visual adicional;
- requiere mapping consistente entre nuestro dominio y BPMN/DI.

Veredicto:

- es la mejor opcion si el requisito es "tener un modelador visual dentro de nuestra app".

## Recomendacion tecnica

### Recomendacion principal

No intentar embeber Bizagi Modeler.

Implementar una arquitectura de dos capas:

- capa 1: nuestra app sigue siendo la dueña del pipeline `PDF -> reconstruccion -> revision -> BPMN`;
- capa 2: Bizagi se usa como consumidor del archivo `BPMN` para modelado posterior, colaboracion y automatizacion.

### Recomendacion para componente visual embebido

Si se necesita un modelador visual dentro de la app, usar `bpmn-js` embebido en una `QWebEngineView` o migrar la UI a una shell web/hibrida.

Motivos:

- `bpmn-js` si esta pensado para embeberse;
- trabaja directamente con `BPMN 2.0 XML`;
- permite visor y modelador;
- evita depender de APIs cerradas de Bizagi;
- mantiene compatibilidad con el flujo de importacion hacia Bizagi.

## Impacto sobre el codigo actual

La base actual ya esta bien orientada para esta estrategia:

- la UI editable vive en `src/pdf_to_bpmn/ui/main_window.py`;
- el canvas actual vive en `src/pdf_to_bpmn/ui/scene.py`;
- el artefacto de interoperabilidad ya existe en `src/pdf_to_bpmn/services/bpmn_semantic.py`.

Eso significa que el camino de menor friccion no es reemplazar la exportacion BPMN, sino fortalecerla y usarla como contrato principal entre nuestra app y Bizagi.

## Plan recomendado por fases

### Fase 1. Endurecer la compatibilidad Bizagi

- validar varios `.bpmn` exportados desde nuestra app en Bizagi Modeler;
- identificar elementos que Bizagi reinterpreta o pierde;
- ajustar `BPMNDI`, pools, lanes, waypoints y tipos BPMN;
- agregar un perfil de exportacion `bizagi-strict`.

### Fase 2. Integracion UX externa

- agregar un comando `Abrir BPMN en Bizagi`;
- detectar si Bizagi esta instalado;
- guardar el `.bpmn` y abrir carpeta/archivo para importacion rapida;
- documentar el flujo `exportar -> importar en Bizagi Modeler -> importar en Bizagi Studio`.

### Fase 3. Editor embebido real

- integrar `QtWebEngine` + `bpmn-js`;
- cargar el `.bpmn` exportado por nuestro backend;
- editar dentro de la app;
- reimportar cambios al dominio local si sigue siendo necesario.

## Decisiones de producto

Si la prioridad es salir rapido:

- seguir con exportacion `.bpmn` hacia Bizagi.

Si la prioridad es una experiencia visual integrada:

- no usar Bizagi como componente UI;
- usar un modelador embebible propio y dejar Bizagi como plataforma de destino.

## Fuentes oficiales consultadas

- Introduccion a Bizagi Modeler service:
  - https://help.bizagi.com/platform/es/intro_welcome.htm
- Importar diagrama desde BPMN:
  - https://help.bizagi.com/platform/es/import_diagram_from_bpmn.htm
- Exchanging processes:
  - https://help.bizagi.com/platform/en/exchanging_processes.htm
- Importing a process model from a BPMN file:
  - https://help.bizagi.com/platform/en/importing-a-process-model-from-bpmn.htm

## Conclusion

Bizagi encaja bien en nuestro flujo como plataforma compatible con `BPMN`, pero no como componente visual embebible oficialmente documentado para una app de terceros.

La decision tecnicamente correcta es:

- corto plazo: integracion por `BPMN`;
- mediano plazo: boton para abrir/transferir a Bizagi;
- largo plazo, si queremos UI embebida: usar un modelador BPMN propio como `bpmn-js`.
