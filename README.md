# metadatos_music

Script para etiquetar lotes de **FLAC** desde la terminal. Toma el número de
pista y el título del **nombre del archivo** y escribe los datos que le pases
por consola: artista, álbum, año, género, etc.

Sin dependencias de Python: solo necesita `metaflac` (paquete `flac`).

## Instalación

```bash
sudo pacman -S flac        # Arch/Manjaro
# sudo apt install flac    # Debian/Ubuntu
```

Opcional, para tenerlo en el PATH:

```bash
ln -s ~/Desarrollo/metadatos_music/tagmusic.py ~/.local/bin/tagmusic
```

## Uso

```bash
# Simula (no escribe nada) y muestra qué cambiaría
./tagmusic.py "~/Música/Guns N' Roses - [1987] -  Appetite for Destruction" \
    "Guns N' Roses" "Appetite for Destruction"

# Aplica los cambios
./tagmusic.py "~/Música/Guns N' Roses - [1987] -  Appetite for Destruction" \
    "Guns N' Roses" "Appetite for Destruction" --apply

# Añadiendo año y género
./tagmusic.py ~/Música/disco "Artista" "Álbum" --date 1987 --genre "Hard Rock" --apply

# Incrustando la carátula (cover.jpg, folder.jpg, front.jpg, portada.jpg...)
./tagmusic.py ~/Música/disco "Artista" "Álbum" --cover --apply

# ...o indicando la imagen a mano
./tagmusic.py ~/Música/disco "Artista" "Álbum" --cover ~/imagenes/portada.png --apply

# Sin argumentos posicionales: pregunta artista, álbum, año y género
./tagmusic.py ~/Música/disco

# Varias carpetas de una vez
./tagmusic.py ~/Música --recursive "Artista" "Álbum" --apply
```

### Qué escribe

| Etiqueta      | Valor                                                        |
| ------------- | ------------------------------------------------------------ |
| `TITLE`       | título deducido del nombre del archivo                        |
| `ARTIST`      | el artista indicado (o el del nombre, si lo tiene)            |
| `ALBUMARTIST` | igual que `ARTIST` salvo `--albumartist`                      |
| `ALBUM`       | el álbum indicado                                             |
| `TRACKNUMBER` | número deducido del nombre (+ `--track-offset`)               |
| `TRACKTOTAL`  | `--total`                                                     |
| `DISCNUMBER`  | `--disc`                                                      |
| `DATE`        | `--date`                                                      |
| `GENRE`       | `--genre`                                                     |
| carátula      | `--cover` (bloque `PICTURE` tipo 3, *Cover (front)*)           |

El resto de etiquetas existentes (`ENCODER`, `RATING`, etc.) no se tocan.

### Carátulas

`--cover` incrusta una imagen como carátula frontal dentro del propio FLAC, así
que cualquier reproductor la muestra aunque el archivo se mueva de carpeta.

- `--cover` sin valor busca la imagen en la carpeta de cada archivo (o en la
  carpeta indicada): `cover`, `folder`, `front`, `album`, `albumart`, `portada`
  o `caratula`, con extensión `.jpg`, `.jpeg` o `.png` (sin distinguir
  mayúsculas).
- `--cover RUTA` usa esa imagen para todos los archivos del lote.
- Solo se reemplaza la carátula **frontal**; otras imágenes (contraportada,
  libreto...) se conservan.
- Si la carátula ya está incrustada y es idéntica, no se reescribe nada.
- Tras escribir se verifica que quede exactamente una frontal con el tipo y
  tamaño esperados.

### Nombres de archivo reconocidos

Por defecto se entiende este formato (el artista es opcional):

```
01. Artista - Título.flac
01 - Título.flac
Título.flac
```

Si tus archivos usan otro formato, pasa un patrón propio con grupos `track`,
`artist` y `title`:

```bash
./tagmusic.py ~/Música/disco "Artista" "Álbum" \
    --pattern '^(?P<track>\d+)_(?P<artist>[^_]+)_(?P<title>.+)$'
```

## Seguridad

- **Modo simulación por defecto**: muestra `antiguo → nuevo` y no escribe hasta
  que uses `--apply` (o respondas `s` a la pregunta final).
- **Respaldo automático**: antes de escribir, exporta las etiquetas originales a
  `~/.local/state/tagmusic/backups/<fecha>/` (`--backup-dir` para cambiarlo,
  `--no-backup` para desactivarlo). Si se cambia la carátula, las imágenes
  anteriores se guardan también como `*.picture-<n>.<ext>`.
- **Verificación**: tras escribir, relee cada archivo y comprueba que las
  etiquetas quedaron como se esperaba.
- Si algún archivo da error (nombre no reconocido, no es FLAC, etc.), se aborta
  el lote completo antes de escribir nada.

## Opciones útiles

| Opción              | Para qué sirve                                          |
| ------------------- | ------------------------------------------------------- |
| `--apply` / `--dry-run` | escribir / solo simular                            |
| `-y`, `--yes`       | no preguntar confirmación                               |
| `-r`, `--recursive` | recorrer subcarpetas                                    |
| `--ext .flac`       | extensión a procesar (repetible)                        |
| `--track-offset N`  | sumar N a las pistas (útil en discos 2)                 |
| `--cover [IMG]`     | incrustar carátula (sin valor: buscarla en la carpeta)   |
| `--no-title`        | no tocar `TITLE`                                        |
| `--show-unchanged`  | listar también archivos que ya estaban bien             |
| `--report ruta.json`| guardar informe de los cambios                          |
| `-q`, `--quiet`     | salida reducida                                         |

## Pruebas

```bash
python3 -m unittest discover -s tests -v
```

Las pruebas generan FLAC de prueba con `ffmpeg` en un directorio temporal, así
que no tocan tu música.
