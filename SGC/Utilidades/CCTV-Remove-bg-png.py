"""
remove_bg.py — Elimina el fondo de todos los PNGs de una carpeta.
Detecta automáticamente el color de fondo desde las esquinas de cada imagen.
Usa flood fill para no tocar partes del objeto con color similar al fondo.

Uso:
    python remove_bg.py                        # procesa ./img/devices/ (in-place)
    python remove_bg.py --carpeta ruta/carpeta
    python remove_bg.py --tolerancia 25        # diferencia max de color (default: 30)
    python remove_bg.py --suavizado 2          # píxeles de borde suave (default: 2)
    python remove_bg.py --sin-subdir           # no procesa subcarpetas
"""

import argparse
from pathlib import Path
import numpy as np
from PIL import Image
from scipy import ndimage


def detectar_color_fondo(arr):
    """Devuelve el color de fondo como el color más frecuente en los 4 bordes."""
    h, w = arr.shape[:2]
    borde = np.concatenate([
        arr[0, :, :3],
        arr[-1, :, :3],
        arr[:, 0, :3],
        arr[:, -1, :3],
    ])
    # Color más frecuente en el borde
    colores, conteos = np.unique(borde.reshape(-1, 3), axis=0, return_counts=True)
    return colores[np.argmax(conteos)].astype(float)


def remove_bg(img_path: Path, tolerancia: int = 10, suavizado: int = 2) -> bool:
    try:
        img = Image.open(img_path).convert("RGBA")
        data = np.array(img, dtype=np.uint8)
        rgb = data[:, :, :3].astype(float)

        fondo_color = detectar_color_fondo(data)

        # Distancia euclidiana de cada pixel al color de fondo
        diff = np.sqrt(np.sum((rgb - fondo_color) ** 2, axis=2))
        mask_similar = diff <= tolerancia

        # Flood fill desde los 4 bordes — solo elimina lo CONECTADO al exterior
        seed = np.zeros_like(mask_similar)
        seed[0, :]  = mask_similar[0, :]
        seed[-1, :] = mask_similar[-1, :]
        seed[:, 0]  = mask_similar[:, 0]
        seed[:, -1] = mask_similar[:, -1]

        fondo = ndimage.binary_propagation(seed, mask=mask_similar)

        # Alpha: fondo = 0, objeto = 255, borde = gradiente suave
        dist = ndimage.distance_transform_edt(~fondo)
        alpha = np.clip(dist / max(suavizado, 1), 0, 1)

        nuevo_alpha = (alpha * 255).astype(np.uint8)
        # Preservar transparencia preexistente
        nuevo_alpha = np.minimum(nuevo_alpha, data[:, :, 3])

        data[:, :, 3] = nuevo_alpha
        Image.fromarray(data, "RGBA").save(img_path, "PNG", optimize=True)
        return True
    except Exception as e:
        print(f"  ✗ Error en {img_path.name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Elimina fondo de PNGs por flood fill")
    parser.add_argument("--carpeta",    default="../img/devices")
    parser.add_argument("--tolerancia", type=int, default=10,
                        help="Diferencia máx de color para considerar 'fondo' (default: 30)")
    parser.add_argument("--suavizado",  type=int, default=2,
                        help="Píxeles de transición en el borde (default: 2)")
    parser.add_argument("--sin-subdir", action="store_true")
    args = parser.parse_args()

    carpeta = Path(args.carpeta)
    if not carpeta.exists():
        print(f"✗ La carpeta '{carpeta}' no existe.")
        return

    patron = "*.png" if args.sin_subdir else "**/*.png"
    archivos = sorted(carpeta.glob(patron))

    if not archivos:
        print(f"No se encontraron PNGs en '{carpeta}'")
        return

    print(f"Procesando {len(archivos)} PNGs (tolerancia={args.tolerancia}, suavizado={args.suavizado})...\n")

    ok = err = skip = 0
    for f in archivos:
        try:
            img = Image.open(f)
            if img.mode == "RGBA":
                arr = np.array(img)
                if arr[:, :, 3].min() < 255:
                    print(f"  ↷ {f.name} — ya tiene transparencia, se omite")
                    skip += 1
                    continue
        except Exception:
            pass

        if remove_bg(f, args.tolerancia, args.suavizado):
            print(f"  ✓ {f.name}")
            ok += 1
        else:
            err += 1

    print(f"\nListo: {ok} procesadas · {skip} omitidas (ya transparentes) · {err} errores.")


if __name__ == "__main__":
    main()
