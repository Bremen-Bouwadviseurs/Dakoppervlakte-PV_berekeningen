import math
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

import requests


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api"
OVERPASS_URLS = [
	"https://overpass-api.de/api/interpreter",
	"https://overpass.kumi.systems/api/interpreter",
	"https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "roof-area-desktop/1.0"


def _base_headers() -> Dict[str, str]:
	return {
		"User-Agent": USER_AGENT,
		"Accept": "application/json",
		"Accept-Language": "nl,en;q=0.9",
		"Referer": "https://www.openstreetmap.org/",
	}


def geocode_with_nominatim(address: str) -> Optional[Dict]:
	params = {
		"q": address,
		"format": "jsonv2",
		"limit": 1,
		"addressdetails": 1,
	}
	response = requests.get(NOMINATIM_URL, params=params, headers=_base_headers(), timeout=20)
	response.raise_for_status()
	items = response.json()
	return items[0] if items else None


def geocode_with_photon(address: str) -> Optional[Dict]:
	params = {
		"q": address,
		"limit": 1,
		"lang": "nl",
	}
	response = requests.get(PHOTON_URL, params=params, headers=_base_headers(), timeout=20)
	response.raise_for_status()
	items = response.json().get("features", [])
	if not items:
		return None

	feature = items[0]
	coords = feature.get("geometry", {}).get("coordinates", [])
	if len(coords) < 2:
		return None

	props = feature.get("properties", {})
	parts = [
		props.get("name"),
		props.get("street"),
		props.get("housenumber"),
		props.get("postcode"),
		props.get("city"),
		props.get("country"),
	]
	display_name = ", ".join(str(p) for p in parts if p)

	return {
		"lat": str(coords[1]),
		"lon": str(coords[0]),
		"display_name": display_name or address,
	}


def geocode_address(address: str) -> Optional[Dict]:
	try:
		result = geocode_with_nominatim(address)
		if result:
			return result
	except requests.HTTPError as exc:
		status_code = exc.response.status_code if exc.response is not None else None
		if status_code not in (403, 429):
			raise

	return geocode_with_photon(address)


def fetch_nearby_buildings(lat: float, lon: float, radius_m: int = 90) -> List[Dict]:
	queries = [
		f"""
		[out:json][timeout:22];
		(
		  way(around:{radius_m},{lat},{lon})["building"];
		  relation(around:{radius_m},{lat},{lon})["building"];
		);
		out geom;
		""",
		f"""
		[out:json][timeout:15];
		way(around:{max(45, radius_m // 2)},{lat},{lon})["building"];
		out geom;
		""",
	]

	last_error: Optional[Exception] = None
	for endpoint in OVERPASS_URLS:
		for query in queries:
			for retry in range(3):
				try:
					response = requests.post(
						endpoint,
						data=query.encode("utf-8"),
						headers=_base_headers(),
						timeout=30,
					)

					if response.status_code in (429, 502, 503, 504):
						raise requests.HTTPError(
							f"{response.status_code} tijdelijke fout op {endpoint}",
							response=response,
						)

					response.raise_for_status()
					return response.json().get("elements", [])
				except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
					last_error = exc
					if retry < 2:
						time.sleep(0.8 * (retry + 1))
					continue

	if last_error:
		raise last_error
	return []


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
	r = 6371000.0
	d_lat = math.radians(lat2 - lat1)
	d_lon = math.radians(lon2 - lon1)
	a = (
		math.sin(d_lat / 2) ** 2
		+ math.cos(math.radians(lat1))
		* math.cos(math.radians(lat2))
		* math.sin(d_lon / 2) ** 2
	)
	return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def close_polygon(coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
	if len(coords) < 3:
		return []
	if coords[0] != coords[-1]:
		return coords + [coords[0]]
	return coords


def polygon_centroid(coords: List[Tuple[float, float]]) -> Tuple[float, float]:
	lats = [c[0] for c in coords]
	lons = [c[1] for c in coords]
	return sum(lats) / len(lats), sum(lons) / len(lons)


def project_to_local_meters(
	lat: float,
	lon: float,
	reference_lat: float,
	reference_lon: float,
) -> Tuple[float, float]:
	lat_scale = 111320.0
	lon_scale = 111320.0 * math.cos(math.radians(reference_lat))
	x_m = (lon - reference_lon) * lon_scale
	y_m = (lat - reference_lat) * lat_scale
	return x_m, y_m


def point_in_polygon(lat: float, lon: float, polygon: List[Tuple[float, float]]) -> bool:
	inside = False
	for i in range(len(polygon) - 1):
		y1, x1 = polygon[i]
		y2, x2 = polygon[i + 1]
		intersects = (x1 > lon) != (x2 > lon)
		if intersects:
			cross_lat = (y2 - y1) * (lon - x1) / (x2 - x1 + 1e-12) + y1
			if lat < cross_lat:
				inside = not inside
	return inside


def polygon_area_m2(
	coords: List[Tuple[float, float]], reference_point: Optional[Tuple[float, float]] = None
) -> float:
	if reference_point is None:
		reference_point = polygon_centroid(coords)

	reference_lat, reference_lon = reference_point
	projected = [
		project_to_local_meters(lat, lon, reference_lat, reference_lon) for lat, lon in coords
	]

	area_2 = 0.0
	for i in range(len(projected) - 1):
		x1, y1 = projected[i]
		x2, y2 = projected[i + 1]
		area_2 += x1 * y2 - x2 * y1

	return abs(area_2) * 0.5


def extract_polygon_from_element(element: Dict) -> List[Tuple[float, float]]:
	geometry = element.get("geometry", [])
	coords = [(p["lat"], p["lon"]) for p in geometry if "lat" in p and "lon" in p]
	return close_polygon(coords)


def choose_best_building(
	lat: float, lon: float, elements: List[Dict]
) -> Optional[List[Tuple[float, float]]]:
	polygons = []
	for element in elements:
		poly = extract_polygon_from_element(element)
		if len(poly) >= 4:
			polygons.append(poly)

	if not polygons:
		return None

	containing = [poly for poly in polygons if point_in_polygon(lat, lon, poly)]
	candidates = containing if containing else polygons

	return min(
		candidates,
		key=lambda poly: haversine_distance_m(lat, lon, *polygon_centroid(poly)),
	)


class RoofDesktopApp:
	def __init__(self, root: tk.Tk) -> None:
		self.root = root
		self.root.title("Dakoppervlakte berekening - Bremen Bouwadviseurs BV")
		self.root.geometry("980x640")
		self.root.minsize(840, 560)
		self.root.configure(bg="#eaf1f7")

		self.last_polygon: Optional[List[Tuple[float, float]]] = None
		self.last_point: Optional[Tuple[float, float]] = None

		self.area_var = tk.StringVar(value="- m2")
		self.address_var = tk.StringVar(value="Nog geen adres opgezocht")
		self.status_var = tk.StringVar(value="Klaar voor invoer")

		self._build_styles()
		self._build_ui()

	def _build_styles(self) -> None:
		style = ttk.Style(self.root)
		style.theme_use("clam")
		style.configure("Panel.TFrame", background="#ffffff")
		style.configure("Surface.TFrame", background="#eef4fb")
		style.configure("Title.TLabel", background="#ffffff", foreground="#123b61", font=("Segoe UI", 18, "bold"))
		style.configure("Body.TLabel", background="#ffffff", foreground="#345066", font=("Segoe UI", 10))
		style.configure("Field.TLabel", background="#ffffff", foreground="#204565", font=("Segoe UI", 10, "bold"))
		style.configure("AreaTitle.TLabel", background="#0076d1", foreground="#ffffff", font=("Segoe UI", 10, "bold"))
		style.configure("AreaValue.TLabel", background="#0076d1", foreground="#ffffff", font=("Segoe UI", 24, "bold"))
		style.configure("Status.TLabel", background="#123b61", foreground="#ffffff", font=("Segoe UI", 9))

	def _build_ui(self) -> None:
		main = ttk.Frame(self.root, style="Surface.TFrame", padding=16)
		main.pack(fill="both", expand=True)

		header = ttk.Frame(main, style="Panel.TFrame", padding=16)
		header.pack(fill="x", pady=(0, 12))
		ttk.Label(header, text="Dakoppervlakte Calculator", style="Title.TLabel").pack(anchor="w")
		ttk.Label(
			header,
			text="Voer een adres in. De app zoekt het gebouw in OpenStreetMap en toont de footprint.",
			style="Body.TLabel",
		).pack(anchor="w", pady=(4, 0))

		content = ttk.Frame(main, style="Surface.TFrame")
		content.pack(fill="both", expand=True)

		left = ttk.Frame(content, style="Panel.TFrame", padding=16)
		left.pack(side="left", fill="y", padx=(0, 10))

		right = ttk.Frame(content, style="Panel.TFrame", padding=10)
		right.pack(side="left", fill="both", expand=True)

		ttk.Label(left, text="Adres", style="Field.TLabel").pack(anchor="w")
		self.address_entry = ttk.Entry(left, width=42, font=("Segoe UI", 10))
		self.address_entry.pack(fill="x", pady=(6, 10))
		self.address_entry.insert(0, "Bijv. Parallelweg 2-B, Heerlen")
		self.address_entry.bind("<FocusIn>", self._clear_placeholder)
		self.address_entry.bind("<Return>", self._on_calculate)

		self.search_btn = ttk.Button(left, text="Bereken dakoppervlakte", command=self._on_calculate)
		self.search_btn.pack(fill="x")

		metric_box = tk.Frame(left, bg="#0076d1", bd=0, highlightthickness=0)
		metric_box.pack(fill="x", pady=16)
		ttk.Label(metric_box, text="Geschatte dakoppervlakte", style="AreaTitle.TLabel").pack(anchor="w", padx=12, pady=(10, 2))
		ttk.Label(metric_box, textvariable=self.area_var, style="AreaValue.TLabel").pack(anchor="w", padx=12, pady=(0, 10))

		ttk.Label(left, text="Gevonden locatie", style="Field.TLabel").pack(anchor="w")
		self.address_out = tk.Label(
			left,
			textvariable=self.address_var,
			justify="left",
			anchor="nw",
			wraplength=300,
			bg="#ffffff",
			fg="#37556f",
			font=("Segoe UI", 9),
		)
		self.address_out.pack(fill="x", pady=(6, 0))

		title = tk.Label(
			right,
			text="Bovenaanzicht gebouwcontour",
			bg="#ffffff",
			fg="#123b61",
			font=("Segoe UI", 12, "bold"),
		)
		title.pack(anchor="w", padx=6, pady=(4, 8))

		self.canvas = tk.Canvas(
			right,
			bg="#f7fbff",
			highlightbackground="#d2e0ee",
			highlightthickness=1,
		)
		self.canvas.pack(fill="both", expand=True)
		self.canvas.bind("<Configure>", self._on_canvas_resize)
		self._draw_placeholder()

		status = ttk.Label(main, textvariable=self.status_var, style="Status.TLabel", anchor="w", padding=(10, 6))
		status.pack(fill="x", pady=(10, 0))

	def _clear_placeholder(self, _event: tk.Event) -> None:
		if self.address_entry.get().strip().lower().startswith("bijv."):
			self.address_entry.delete(0, "end")

	def _on_calculate(self, _event: Optional[tk.Event] = None) -> None:
		address = self.address_entry.get().strip()
		if not address or address.lower().startswith("bijv."):
			messagebox.showwarning("Adres ontbreekt", "Vul eerst een geldig adres in.")
			return

		self.search_btn.configure(state="disabled")
		self.status_var.set("Adres en gebouwdata ophalen...")
		worker = threading.Thread(target=self._worker_calculate, args=(address,), daemon=True)
		worker.start()

	def _worker_calculate(self, address: str) -> None:
		try:
			location = geocode_address(address)
			if not location:
				raise ValueError("Adres niet gevonden. Probeer een completer adres.")

			lat = float(location["lat"])
			lon = float(location["lon"])
			elements = fetch_nearby_buildings(lat, lon)
			polygon = choose_best_building(lat, lon, elements)
			if not polygon:
				raise ValueError("Geen gebouwcontour gevonden in de buurt van dit adres.")

			area_m2 = polygon_area_m2(polygon, (lat, lon))
			display_name = location.get("display_name", "Onbekende locatie")
			self.root.after(0, self._show_result, area_m2, display_name, polygon, (lat, lon))
		except requests.HTTPError as exc:
			status_code = exc.response.status_code if exc.response is not None else None
			if status_code in (429, 502, 503, 504):
				self.root.after(
					0,
					self._show_error,
					"Kaartserver is tijdelijk druk of niet bereikbaar. Probeer het over 10-30 seconden opnieuw.",
				)
			else:
				self.root.after(0, self._show_error, f"Fout bij kaartdata: {exc}")
		except requests.RequestException as exc:
			self.root.after(0, self._show_error, f"Netwerkfout: {exc}")
		except Exception as exc:
			self.root.after(0, self._show_error, str(exc))

	def _show_result(
		self,
		area_m2: float,
		display_name: str,
		polygon: List[Tuple[float, float]],
		point: Tuple[float, float],
	) -> None:
		formatted = f"{area_m2:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
		self.area_var.set(f"{formatted} m2")
		self.address_var.set(display_name)
		self.status_var.set("Berekening gereed")
		self.last_polygon = polygon
		self.last_point = point
		self._draw_polygon(polygon, point)
		self.search_btn.configure(state="normal")

	def _show_error(self, message: str) -> None:
		self.status_var.set("Fout opgetreden")
		self.search_btn.configure(state="normal")
		messagebox.showerror("Kan niet berekenen", message)

	def _on_canvas_resize(self, _event: tk.Event) -> None:
		if self.last_polygon and self.last_point:
			self._draw_polygon(self.last_polygon, self.last_point)
		else:
			self._draw_placeholder()

	def _draw_placeholder(self) -> None:
		self.canvas.delete("all")
		w = max(self.canvas.winfo_width(), 200)
		h = max(self.canvas.winfo_height(), 180)
		self.canvas.create_text(
			w / 2,
			h / 2,
			text="Zoek een adres om de gebouwcontour te tonen",
			fill="#6a8299",
			font=("Segoe UI", 11),
		)

	def _draw_polygon(self, polygon: List[Tuple[float, float]], point: Tuple[float, float]) -> None:
		self.canvas.delete("all")
		w = max(self.canvas.winfo_width(), 240)
		h = max(self.canvas.winfo_height(), 200)

		self._draw_grid(w, h)

		reference_lat, reference_lon = point
		points = [
			project_to_local_meters(lat, lon, reference_lat, reference_lon)
			for lat, lon in polygon[:-1]
		]
		x_vals = [p[0] for p in points]
		y_vals = [p[1] for p in points]

		if not x_vals or not y_vals:
			self._draw_placeholder()
			return

		min_x, max_x = min(x_vals), max(x_vals)
		min_y, max_y = min(y_vals), max(y_vals)

		dx = max(max_x - min_x, 0.00001)
		dy = max(max_y - min_y, 0.00001)
		padding = 30
		scale = min((w - 2 * padding) / dx, (h - 2 * padding) / dy)

		def to_canvas(x_m: float, y_m: float) -> Tuple[float, float]:
			x = padding + (x_m - min_x) * scale
			y = h - (padding + (y_m - min_y) * scale)
			return x, y

		canvas_coords = []
		for x_m, y_m in points:
			x, y = to_canvas(x_m, y_m)
			canvas_coords.extend([x, y])

		self.canvas.create_polygon(
			canvas_coords,
			fill="#69b3f2",
			outline="#14548e",
			width=2,
			stipple="gray25",
		)

		point_x, point_y = to_canvas(0.0, 0.0)
		r = 5
		self.canvas.create_oval(point_x - r, point_y - r, point_x + r, point_y + r, fill="#e91e63", outline="#9f1242")

		self.canvas.create_text(12, 12, anchor="nw", text="Noord", fill="#43617d", font=("Segoe UI", 9, "bold"))
		self.canvas.create_line(52, 28, 52, 12, fill="#43617d", width=2, arrow="last")

	def _draw_grid(self, width: int, height: int) -> None:
		step = 36
		for x in range(0, width, step):
			self.canvas.create_line(x, 0, x, height, fill="#edf3f9")
		for y in range(0, height, step):
			self.canvas.create_line(0, y, width, y, fill="#edf3f9")


def main() -> None:
	root = tk.Tk()
	RoofDesktopApp(root)
	root.mainloop()


if __name__ == "__main__":
	main()


