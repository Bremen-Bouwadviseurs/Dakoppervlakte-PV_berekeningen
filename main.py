import math
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageTk


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


def _orientation_csv_candidates() -> List[Path]:
	base = Path(__file__).resolve().parent
	return [
		base / "Config" / "orientatiepv.csv",
		base / "Conig" / "orientatiepv.csv",
	]


def load_available_orientations() -> List[float]:
	for path in _orientation_csv_candidates():
		if not path.exists():
			continue
		try:
			first_line = path.read_text(encoding="utf-8").splitlines()[0]
			values = [v.strip() for v in first_line.split(";")[1:] if v.strip()]
			parsed = sorted({float(v) for v in values})
			if parsed:
				return parsed
		except Exception:
			continue
	return [float(v) for v in range(0, 360, 5)]


def rotate_point(x: float, y: float, angle_deg: float) -> Tuple[float, float]:
	angle = math.radians(angle_deg)
	c = math.cos(angle)
	s = math.sin(angle)
	return x * c - y * s, x * s + y * c


def point_on_segment(
	px: float,
	py: float,
	ax: float,
	ay: float,
	bx: float,
	by: float,
	eps: float = 1e-7,
) -> bool:
	cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
	if abs(cross) > eps:
		return False
	dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
	if dot < -eps:
		return False
	length_sq = (bx - ax) ** 2 + (by - ay) ** 2
	if dot - length_sq > eps:
		return False
	return True


def point_in_polygon_xy(x: float, y: float, polygon: List[Tuple[float, float]]) -> bool:
	n = len(polygon)
	if n < 3:
		return False

	inside = False
	j = n - 1
	for i in range(n):
		xi, yi = polygon[i]
		xj, yj = polygon[j]

		if point_on_segment(x, y, xi, yi, xj, yj):
			return True

		intersects = ((yi > y) != (yj > y)) and (
			x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-12) + xi
		)
		if intersects:
			inside = not inside
		j = i

	return inside


def point_to_segment_distance_m(
	px: float,
	py: float,
	ax: float,
	ay: float,
	bx: float,
	by: float,
) -> float:
	vx = bx - ax
	vy = by - ay
	wx = px - ax
	wy = py - ay
	seg_len_sq = vx * vx + vy * vy
	if seg_len_sq <= 1e-12:
		return math.hypot(px - ax, py - ay)
	t = max(0.0, min(1.0, (wx * vx + wy * vy) / seg_len_sq))
	proj_x = ax + t * vx
	proj_y = ay + t * vy
	return math.hypot(px - proj_x, py - proj_y)


def min_distance_to_polygon_edges_m(
	x: float,
	y: float,
	polygon: List[Tuple[float, float]],
) -> float:
	if len(polygon) < 2:
		return 0.0

	min_dist = float("inf")
	for i in range(len(polygon)):
		ax, ay = polygon[i]
		bx, by = polygon[(i + 1) % len(polygon)]
		dist = point_to_segment_distance_m(x, y, ax, ay, bx, by)
		if dist < min_dist:
			min_dist = dist
	return min_dist


def _orientation_2d(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
	return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_intersect(
	ax: float,
	ay: float,
	bx: float,
	by: float,
	cx: float,
	cy: float,
	dx: float,
	dy: float,
	eps: float = 1e-9,
) -> bool:
	o1 = _orientation_2d(ax, ay, bx, by, cx, cy)
	o2 = _orientation_2d(ax, ay, bx, by, dx, dy)
	o3 = _orientation_2d(cx, cy, dx, dy, ax, ay)
	o4 = _orientation_2d(cx, cy, dx, dy, bx, by)

	if (o1 > eps and o2 < -eps or o1 < -eps and o2 > eps) and (
		o3 > eps and o4 < -eps or o3 < -eps and o4 > eps
	):
		return True

	if abs(o1) <= eps and point_on_segment(cx, cy, ax, ay, bx, by, eps):
		return True
	if abs(o2) <= eps and point_on_segment(dx, dy, ax, ay, bx, by, eps):
		return True
	if abs(o3) <= eps and point_on_segment(ax, ay, cx, cy, dx, dy, eps):
		return True
	if abs(o4) <= eps and point_on_segment(bx, by, cx, cy, dx, dy, eps):
		return True

	return False


def point_in_axis_aligned_rect(
	px: float,
	py: float,
	rect: Tuple[float, float, float, float],
) -> bool:
	min_x, min_y, max_x, max_y = rect
	return min_x <= px <= max_x and min_y <= py <= max_y


def panel_intersects_no_go(
	panel: List[Tuple[float, float]],
	no_go_polygons: List[List[Tuple[float, float]]],
) -> bool:
	if len(panel) < 3:
		return False

	panel_edges = [
		(panel[i], panel[(i + 1) % len(panel)])
		for i in range(len(panel))
	]

	for no_go in no_go_polygons:
		if len(no_go) < 3:
			continue

		no_go_edges = [
			(no_go[i], no_go[(i + 1) % len(no_go)])
			for i in range(len(no_go))
		]

		if any(point_in_polygon_xy(px, py, no_go) for px, py in panel):
			return True

		if any(point_in_polygon_xy(nx, ny, panel) for nx, ny in no_go):
			return True

		for (p1, p2) in panel_edges:
			for (n1, n2) in no_go_edges:
				if segments_intersect(
					p1[0], p1[1], p2[0], p2[1], n1[0], n1[1], n2[0], n2[1]
				):
					return True

	return False


def compute_panel_layout(
	polygon_xy: List[Tuple[float, float]],
	panel_length_m: float,
	panel_width_m: float,
	orientation_deg: float,
	spacing_m: float = 0.2,
	edge_clearance_m: float = 2.0,
	no_go_polygons: Optional[List[List[Tuple[float, float]]]] = None,
) -> List[List[Tuple[float, float]]]:
	if len(polygon_xy) < 3 or panel_length_m <= 0 or panel_width_m <= 0:
		return []
	if no_go_polygons is None:
		no_go_polygons = []

	if polygon_xy[0] == polygon_xy[-1]:
		poly = polygon_xy[:-1]
	else:
		poly = polygon_xy[:]

	rotated_poly = [rotate_point(x, y, -orientation_deg) for x, y in poly]

	min_x = min(p[0] for p in rotated_poly) + edge_clearance_m
	max_x = max(p[0] for p in rotated_poly) - edge_clearance_m
	min_y = min(p[1] for p in rotated_poly) + edge_clearance_m
	max_y = max(p[1] for p in rotated_poly) - edge_clearance_m

	if min_x >= max_x or min_y >= max_y:
		return []

	step_x = panel_length_m + spacing_m
	step_y = panel_width_m + spacing_m

	panels: List[List[Tuple[float, float]]] = []
	y = min_y
	while y + panel_width_m <= max_y + 1e-9:
		x = min_x
		while x + panel_length_m <= max_x + 1e-9:
			rect = [
				(x, y),
				(x + panel_length_m, y),
				(x + panel_length_m, y + panel_width_m),
				(x, y + panel_width_m),
			]
			rect_center = (x + panel_length_m * 0.5, y + panel_width_m * 0.5)
			check_points = rect + [rect_center]
			if all(point_in_polygon_xy(px, py, rotated_poly) for px, py in check_points) and all(
				min_distance_to_polygon_edges_m(px, py, rotated_poly) >= edge_clearance_m - 1e-9
				for px, py in check_points
			):
				panel_world = [rotate_point(px, py, orientation_deg) for px, py in rect]
				if not panel_intersects_no_go(panel_world, no_go_polygons):
					panels.append(panel_world)
			x += step_x
		y += step_y

	return panels


class RoofDesktopApp:
	def __init__(self, root: tk.Tk) -> None:
		self.root = root
		self.root.title("Dakoppervlakte en zonnepanelen - Bremen Bouwadviseurs BV")
		self.root.geometry("1080x700")
		self.root.minsize(920, 820)
		self.root.configure(bg="#eaf1f7")

		self.available_orientations = load_available_orientations()

		self.last_polygon_xy: Optional[List[Tuple[float, float]]] = None
		self.last_context_polygons_xy: List[List[Tuple[float, float]]] = []
		self.last_panels: List[List[Tuple[float, float]]] = []
		self.last_orientation_deg: float = 0.0
		self.last_panel_length_m: float = 2.0
		self.last_panel_width_m: float = 1.0

		self.no_go_polygons: List[List[Tuple[float, float]]] = []
		self.no_go_mode_var = tk.BooleanVar(value=False)
		self.edge_pick_mode_var = tk.BooleanVar(value=False)
		self.selected_roof_edge: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None

		self.view_zoom = 1.0
		self.view_pan_x = 0.0
		self.view_pan_y = 0.0
		self.fit_min_x = 0.0
		self.fit_min_y = 0.0
		self.fit_scale = 1.0
		self.fit_padding = 30.0
		self.fit_canvas_h = 1.0

		self.is_panning = False
		self.pan_start_canvas: Optional[Tuple[float, float]] = None
		self.pan_start_offset: Optional[Tuple[float, float]] = None

		self.is_drawing_no_go = False
		self.no_go_start_canvas: Optional[Tuple[float, float]] = None
		self.no_go_current_canvas: Optional[Tuple[float, float]] = None

		self.area_var = tk.StringVar(value="- m2")
		self.address_var = tk.StringVar(value="Nog geen adres opgezocht")
		self.status_var = tk.StringVar(value="Klaar voor invoer")
		self.panel_count_var = tk.StringVar(value="0 panelen")

		self.panel_length_var = tk.StringVar(value="2.0")
		self.panel_width_var = tk.StringVar(value="1.0")
		self.orientation_var = tk.StringVar(value=self._default_orientation_text())

		self._load_logo()
		self._build_styles()
		self._build_ui()
		self._update_canvas_cursor()

	def _load_logo(self) -> None:
		try:
			logo_path = Path(__file__).parent / "bba-logo.png"
			if logo_path.exists():
				logo_img = Image.open(logo_path)
				logo_img.thumbnail((80, 80), Image.Resampling.LANCZOS)
				self.logo_photo = ImageTk.PhotoImage(logo_img)
			else:
				self.logo_photo = None
		except Exception:
			self.logo_photo = None

	def _default_orientation_text(self) -> str:
		if self.available_orientations:
			mid = len(self.available_orientations) // 2
			value = self.available_orientations[mid]
			return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"
		return "180"

	def _build_styles(self) -> None:
		style = ttk.Style(self.root)
		style.theme_use("clam")
		style.configure("Panel.TFrame", background="#ffffff")
		style.configure("Surface.TFrame", background="#eef4fb")
		style.configure(
			"Title.TLabel",
			background="#ffffff",
			foreground="#123b61",
			font=("Segoe UI", 18, "bold"),
		)
		style.configure(
			"Body.TLabel",
			background="#ffffff",
			foreground="#345066",
			font=("Segoe UI", 10),
		)
		style.configure(
			"Field.TLabel",
			background="#ffffff",
			foreground="#204565",
			font=("Segoe UI", 10, "bold"),
		)
		style.configure(
			"AreaTitle.TLabel",
			background="#0076d1",
			foreground="#ffffff",
			font=("Segoe UI", 10, "bold"),
		)
		style.configure(
			"AreaValue.TLabel",
			background="#0076d1",
			foreground="#ffffff",
			font=("Segoe UI", 24, "bold"),
		)
		style.configure(
			"Status.TLabel",
			background="#123b61",
			foreground="#ffffff",
			font=("Segoe UI", 9),
		)
		style.configure(
			"PanelCount.TLabel",
			background="#ffffff",
			foreground="#aa2020",
			font=("Segoe UI", 11, "bold"),
		)

	def _build_ui(self) -> None:
		main = ttk.Frame(self.root, style="Surface.TFrame", padding=16)
		main.pack(fill="both", expand=True)

		header = ttk.Frame(main, style="Panel.TFrame", padding=16)
		header.pack(fill="x", pady=(0, 12))
		
		# Logo en titel naast elkaar
		header_top = ttk.Frame(header, style="Panel.TFrame")
		header_top.pack(anchor="w")
		
		if self.logo_photo:
			logo_label = tk.Label(header_top, image=self.logo_photo, bg="#ffffff")
			logo_label.pack(side="left", padx=(0, 16))
		
		text_frame = ttk.Frame(header_top, style="Panel.TFrame")
		text_frame.pack(side="left", fill="x", expand=True)
		
		ttk.Label(text_frame, text="Dakoppervlakte Calculator", style="Title.TLabel").pack(anchor="w")
		ttk.Label(
			text_frame,
			text=(
				"Voer een adres in, kies paneelmaat en orientatie. "
				"Zoom met het muiswiel, sleep om te bewegen en teken no-go zones in rood."
			),
			style="Body.TLabel",
		).pack(anchor="w", pady=(4, 0))

		content = ttk.Frame(main, style="Surface.TFrame")
		content.pack(fill="both", expand=True)

		left = ttk.Frame(content, style="Panel.TFrame", padding=16)
		left.pack(side="left", fill="y", padx=(0, 10))

		right = ttk.Frame(content, style="Panel.TFrame", padding=10)
		right.pack(side="left", fill="both", expand=True)

		ttk.Label(left, text="Adres", style="Field.TLabel").pack(anchor="w")
		self.address_entry = ttk.Entry(left, width=44, font=("Segoe UI", 10))
		self.address_entry.pack(fill="x", pady=(6, 10))
		self.address_entry.insert(0, "Bijv. Parallelweg 2-B, Heerlen")
		self.address_entry.bind("<FocusIn>", self._clear_placeholder)
		self.address_entry.bind("<Return>", self._on_calculate)

		ttk.Label(left, text="Paneellengte (m)", style="Field.TLabel").pack(anchor="w", pady=(6, 0))
		ttk.Entry(left, textvariable=self.panel_length_var, width=14).pack(anchor="w", pady=(4, 8))

		ttk.Label(left, text="Paneelbreedte (m)", style="Field.TLabel").pack(anchor="w")
		ttk.Entry(left, textvariable=self.panel_width_var, width=14).pack(anchor="w", pady=(4, 8))

		ttk.Label(left, text="Orientatie panelen (graden)", style="Field.TLabel").pack(anchor="w")
		self.orientation_combo = ttk.Combobox(
			left,
			textvariable=self.orientation_var,
			values=[self._fmt_orientation_value(v) for v in self.available_orientations],
			width=12,
		)
		self.orientation_combo.pack(anchor="w", pady=(4, 12))

		self.search_btn = ttk.Button(
			left,
			text="Bereken dakoppervlakte + panelen",
			command=self._on_calculate,
		)
		self.search_btn.pack(fill="x")

		ttk.Checkbutton(
			left,
			text="Teken no-go zones (rood)",
			variable=self.no_go_mode_var,
			command=self._on_toggle_no_go_mode,
		).pack(anchor="w", pady=(10, 4))

		ttk.Checkbutton(
			left,
			text="Kies dakrand voor orientatie",
			variable=self.edge_pick_mode_var,
			command=self._on_toggle_edge_pick_mode,
		).pack(anchor="w", pady=(0, 6))

		ttk.Button(left, text="Wis no-go zones", command=self._on_clear_no_go_zones).pack(fill="x", pady=(0, 4))
		ttk.Button(left, text="Reset weergave", command=self._reset_view).pack(fill="x")

		metric_box = tk.Frame(left, bg="#0076d1", bd=0, highlightthickness=0)
		metric_box.pack(fill="x", pady=16)
		ttk.Label(metric_box, text="Dakoppervlakte", style="AreaTitle.TLabel").pack(
			anchor="w", padx=12, pady=(10, 2)
		)
		ttk.Label(metric_box, textvariable=self.area_var, style="AreaValue.TLabel").pack(
			anchor="w", padx=12, pady=(0, 10)
		)

		ttk.Label(left, text="Aantal panelen", style="Field.TLabel").pack(anchor="w")
		ttk.Label(left, textvariable=self.panel_count_var, style="PanelCount.TLabel").pack(
			anchor="w", pady=(4, 10)
		)

		ttk.Label(left, text="Gevonden locatie", style="Field.TLabel").pack(anchor="w")
		self.address_out = tk.Label(
			left,
			textvariable=self.address_var,
			justify="left",
			anchor="nw",
			wraplength=320,
			bg="#ffffff",
			fg="#37556f",
			font=("Segoe UI", 9),
		)
		self.address_out.pack(fill="x", pady=(6, 0))

		title = tk.Label(
			right,
			text="Bovenaanzicht gebouwcontour + panelen",
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
		self.canvas.bind("<MouseWheel>", self._on_canvas_mouse_wheel)
		self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
		self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
		self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
		self._draw_placeholder()

		status = ttk.Label(main, textvariable=self.status_var, style="Status.TLabel", anchor="w", padding=(10, 6))
		status.pack(fill="x", pady=(10, 0))

	@staticmethod
	def _fmt_orientation_value(value: float) -> str:
		return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"

	def _clear_placeholder(self, _event: tk.Event) -> None:
		if self.address_entry.get().strip().lower().startswith("bijv."):
			self.address_entry.delete(0, "end")

	def _on_calculate(self, _event: Optional[tk.Event] = None) -> None:
		address = self.address_entry.get().strip()
		if not address or address.lower().startswith("bijv."):
			messagebox.showwarning("Adres ontbreekt", "Vul eerst een geldig adres in.")
			return

		try:
			panel_length_m = float(self.panel_length_var.get().replace(",", "."))
			panel_width_m = float(self.panel_width_var.get().replace(",", "."))
			orientation_deg = float(self.orientation_var.get().replace(",", "."))
		except ValueError:
			messagebox.showwarning(
				"Onjuiste invoer",
				"Paneelafmetingen en orientatie moeten numerieke waarden zijn.",
			)
			return

		if panel_length_m <= 0 or panel_width_m <= 0:
			messagebox.showwarning("Onjuiste invoer", "Paneelafmetingen moeten groter zijn dan 0.")
			return

		self.search_btn.configure(state="disabled")
		self.status_var.set("Adres, gebouwdata en paneelindeling ophalen...")
		worker = threading.Thread(
			target=self._worker_calculate,
			args=(address, panel_length_m, panel_width_m, orientation_deg),
			daemon=True,
		)
		worker.start()

	def _worker_calculate(
		self,
		address: str,
		panel_length_m: float,
		panel_width_m: float,
		orientation_deg: float,
	) -> None:
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
			polygon_xy = [
				project_to_local_meters(p_lat, p_lon, lat, lon) for p_lat, p_lon in polygon[:-1]
			]
			context_polygons_xy: List[List[Tuple[float, float]]] = []
			for element in elements:
				poly = extract_polygon_from_element(element)
				if len(poly) < 4 or poly == polygon:
					continue
				context_polygons_xy.append(
					[project_to_local_meters(p_lat, p_lon, lat, lon) for p_lat, p_lon in poly[:-1]]
				)

			panels = compute_panel_layout(
				polygon_xy,
				panel_length_m=panel_length_m,
				panel_width_m=panel_width_m,
				orientation_deg=orientation_deg,
				spacing_m=0.2,
				edge_clearance_m=2.0,
				no_go_polygons=[],
			)

			display_name = location.get("display_name", "Onbekende locatie")
			self.root.after(
				0,
				self._show_result,
				area_m2,
				display_name,
				polygon_xy,
				context_polygons_xy,
				panels,
				orientation_deg,
				panel_length_m,
				panel_width_m,
			)
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
		polygon_xy: List[Tuple[float, float]],
		context_polygons_xy: List[List[Tuple[float, float]]],
		panels: List[List[Tuple[float, float]]],
		orientation_deg: float,
		panel_length_m: float,
		panel_width_m: float,
	) -> None:
		formatted = f"{area_m2:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
		self.area_var.set(f"{formatted} m2")
		self.panel_count_var.set(f"{len(panels)} panelen")
		self.address_var.set(display_name)
		self.status_var.set("Berekening gereed")
		self.last_polygon_xy = polygon_xy
		self.last_context_polygons_xy = context_polygons_xy
		self.last_panels = panels
		self.last_orientation_deg = orientation_deg
		self.last_panel_length_m = panel_length_m
		self.last_panel_width_m = panel_width_m
		self.selected_roof_edge = None
		self.no_go_polygons = []
		self.no_go_mode_var.set(False)
		self.edge_pick_mode_var.set(False)
		self._reset_view(redraw=False)
		self._update_canvas_cursor()
		self._draw_scene()
		self.search_btn.configure(state="normal")

	def _show_error(self, message: str) -> None:
		self.status_var.set("Fout opgetreden")
		self.search_btn.configure(state="normal")
		messagebox.showerror("Kan niet berekenen", message)

	def _on_canvas_resize(self, _event: tk.Event) -> None:
		self._draw_scene()

	def _on_toggle_no_go_mode(self) -> None:
		if self.no_go_mode_var.get():
			self.edge_pick_mode_var.set(False)
			self.status_var.set("No-go modus actief: sleep in de kaart om een rood vak te tekenen.")
		else:
			self.status_var.set("No-go modus uit. Sleep in de kaart om te pannen.")
		self._update_canvas_cursor()

	def _on_toggle_edge_pick_mode(self) -> None:
		if self.edge_pick_mode_var.get():
			self.no_go_mode_var.set(False)
			self.status_var.set("Randselectie actief: klik op een dakrand om panelen parallel te zetten.")
		else:
			self.status_var.set("Randselectie uit. Sleep in de kaart om te pannen.")
		self._update_canvas_cursor()

	def _on_clear_no_go_zones(self) -> None:
		if not self.no_go_polygons:
			return
		self.no_go_polygons = []
		self._recompute_panels()
		self.status_var.set("No-go zones verwijderd.")

	def _create_oriented_no_go_polygon(
		self,
		start_world: Tuple[float, float],
		end_world: Tuple[float, float],
		orientation_deg: float,
	) -> List[Tuple[float, float]]:
		sx, sy = start_world
		ex, ey = end_world

		rsx, rsy = rotate_point(sx, sy, -orientation_deg)
		rex, rey = rotate_point(ex, ey, -orientation_deg)

		min_x = min(rsx, rex)
		max_x = max(rsx, rex)
		min_y = min(rsy, rey)
		max_y = max(rsy, rey)

		rotated_rect = [
			(min_x, min_y),
			(max_x, min_y),
			(max_x, max_y),
			(min_x, max_y),
		]
		return [rotate_point(px, py, orientation_deg) for px, py in rotated_rect]

	def _reset_view(self, redraw: bool = True) -> None:
		self.view_zoom = 1.0
		self.view_pan_x = 0.0
		self.view_pan_y = 0.0
		if redraw:
			self._draw_scene()

	def _update_canvas_cursor(self) -> None:
		if self.no_go_mode_var.get() or self.edge_pick_mode_var.get():
			self.canvas.configure(cursor="crosshair")
		else:
			self.canvas.configure(cursor="arrow")

	def _on_canvas_mouse_wheel(self, event: tk.Event) -> None:
		if not self.last_polygon_xy:
			return

		delta = getattr(event, "delta", 0)
		if delta == 0:
			return

		factor = 1.12 if delta > 0 else (1.0 / 1.12)
		old_zoom = self.view_zoom
		new_zoom = min(8.0, max(0.35, old_zoom * factor))
		if abs(new_zoom - old_zoom) < 1e-12:
			return

		w = max(self.canvas.winfo_width(), 260)
		h = max(self.canvas.winfo_height(), 220)
		cx = w * 0.5
		cy = h * 0.5
		mx = float(event.x)
		my = float(event.y)

		self.view_pan_x = (mx - cx) - ((mx - cx - self.view_pan_x) / old_zoom) * new_zoom
		self.view_pan_y = (my - cy) - ((my - cy - self.view_pan_y) / old_zoom) * new_zoom
		self.view_zoom = new_zoom
		self._draw_scene()

	def _on_canvas_press(self, event: tk.Event) -> None:
		if not self.last_polygon_xy:
			return

		if self.edge_pick_mode_var.get():
			self._pick_roof_edge_orientation(float(event.x), float(event.y))
			return

		if self.no_go_mode_var.get():
			self.is_drawing_no_go = True
			self.no_go_start_canvas = (float(event.x), float(event.y))
			self.no_go_current_canvas = (float(event.x), float(event.y))
			self._draw_scene()
			return

		self.is_panning = True
		self.pan_start_canvas = (float(event.x), float(event.y))
		self.pan_start_offset = (self.view_pan_x, self.view_pan_y)

	def _on_canvas_drag(self, event: tk.Event) -> None:
		if self.is_drawing_no_go and self.no_go_start_canvas is not None:
			self.no_go_current_canvas = (float(event.x), float(event.y))
			self._draw_scene()
			return

		if self.is_panning and self.pan_start_canvas and self.pan_start_offset:
			dx = float(event.x) - self.pan_start_canvas[0]
			dy = float(event.y) - self.pan_start_canvas[1]
			self.view_pan_x = self.pan_start_offset[0] + dx
			self.view_pan_y = self.pan_start_offset[1] + dy
			self._draw_scene()

	def _on_canvas_release(self, event: tk.Event) -> None:
		if self.is_drawing_no_go and self.no_go_start_canvas is not None:
			self.no_go_current_canvas = (float(event.x), float(event.y))
			start_world = self._canvas_to_world(*self.no_go_start_canvas)
			end_world = self._canvas_to_world(*self.no_go_current_canvas)

			if start_world and end_world:
				no_go_poly = self._create_oriented_no_go_polygon(
					start_world,
					end_world,
					self.last_orientation_deg,
				)

				edge_a = no_go_poly[0]
				edge_b = no_go_poly[1]
				edge_d = no_go_poly[3]
				width_m = math.hypot(edge_b[0] - edge_a[0], edge_b[1] - edge_a[1])
				height_m = math.hypot(edge_d[0] - edge_a[0], edge_d[1] - edge_a[1])

				if width_m >= 0.1 and height_m >= 0.1:
					self.no_go_polygons.append(no_go_poly)
					self._recompute_panels()
					self.status_var.set("No-go zone toegevoegd.")

			self.is_drawing_no_go = False
			self.no_go_start_canvas = None
			self.no_go_current_canvas = None
			self._draw_scene()
			return

		self.is_panning = False
		self.pan_start_canvas = None
		self.pan_start_offset = None

	def _scene_bounds_xy(self) -> Optional[Tuple[float, float, float, float]]:
		if not self.last_polygon_xy:
			return None

		x_vals = [p[0] for p in self.last_polygon_xy]
		y_vals = [p[1] for p in self.last_polygon_xy]
		min_x, max_x = min(x_vals), max(x_vals)
		min_y, max_y = min(y_vals), max(y_vals)

		margin = 6.0
		return min_x - margin, min_y - margin, max_x + margin, max_y + margin

	def _pick_roof_edge_orientation(self, canvas_x: float, canvas_y: float) -> None:
		if not self.last_polygon_xy:
			return

		world_pt = self._canvas_to_world(canvas_x, canvas_y)
		if world_pt is None:
			return

		pixel_to_meter = 1.0 / max(self.fit_scale * self.view_zoom, 1e-9)
		threshold_m = 18.0 * pixel_to_meter

		best_seg: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
		best_dist = float("inf")
		for i in range(len(self.last_polygon_xy)):
			a = self.last_polygon_xy[i]
			b = self.last_polygon_xy[(i + 1) % len(self.last_polygon_xy)]
			dist = point_to_segment_distance_m(world_pt[0], world_pt[1], a[0], a[1], b[0], b[1])
			if dist < best_dist:
				best_dist = dist
				best_seg = (a, b)

		if best_seg is None or best_dist > threshold_m:
			self.status_var.set("Klik dichter bij een dakrand om orientatie te kiezen.")
			return

		dx = best_seg[1][0] - best_seg[0][0]
		dy = best_seg[1][1] - best_seg[0][1]
		angle_deg = (math.degrees(math.atan2(dy, dx)) + 360.0) % 180.0

		self.selected_roof_edge = best_seg
		self.last_orientation_deg = angle_deg
		self.orientation_var.set(self._fmt_orientation_value(angle_deg))
		self.edge_pick_mode_var.set(False)
		self._update_canvas_cursor()
		self._recompute_panels()
		self.status_var.set(f"Orientatie aangepast op dakrand: {angle_deg:.1f} graden")

	def _setup_fit_transform(self, bounds: Tuple[float, float, float, float], w: float, h: float) -> None:
		min_x, min_y, max_x, max_y = bounds
		dx = max(max_x - min_x, 0.00001)
		dy = max(max_y - min_y, 0.00001)
		padding = 30.0
		self.fit_scale = min((w - 2 * padding) / dx, (h - 2 * padding) / dy)
		self.fit_min_x = min_x
		self.fit_min_y = min_y
		self.fit_padding = padding
		self.fit_canvas_h = h

	def _world_to_canvas(self, x_m: float, y_m: float, w: float, h: float) -> Tuple[float, float]:
		x = self.fit_padding + (x_m - self.fit_min_x) * self.fit_scale
		y = self.fit_canvas_h - (self.fit_padding + (y_m - self.fit_min_y) * self.fit_scale)

		cx = w * 0.5
		cy = h * 0.5
		x = (x - cx) * self.view_zoom + cx + self.view_pan_x
		y = (y - cy) * self.view_zoom + cy + self.view_pan_y
		return x, y

	def _canvas_to_world(self, x: float, y: float) -> Optional[Tuple[float, float]]:
		if self.fit_scale <= 0:
			return None

		w = max(self.canvas.winfo_width(), 260)
		h = max(self.canvas.winfo_height(), 220)
		cx = w * 0.5
		cy = h * 0.5

		unzoom_x = ((x - self.view_pan_x) - cx) / self.view_zoom + cx
		unzoom_y = ((y - self.view_pan_y) - cy) / self.view_zoom + cy

		world_x = (unzoom_x - self.fit_padding) / self.fit_scale + self.fit_min_x
		world_y = ((self.fit_canvas_h - unzoom_y) - self.fit_padding) / self.fit_scale + self.fit_min_y
		return world_x, world_y

	def _recompute_panels(self) -> None:
		if not self.last_polygon_xy:
			return

		self.last_panels = compute_panel_layout(
			self.last_polygon_xy,
			panel_length_m=self.last_panel_length_m,
			panel_width_m=self.last_panel_width_m,
			orientation_deg=self.last_orientation_deg,
			spacing_m=0.2,
			edge_clearance_m=2.0,
			no_go_polygons=self.no_go_polygons,
		)
		self.panel_count_var.set(f"{len(self.last_panels)} panelen")
		self._draw_scene()

	def _draw_scene(self) -> None:
		if self.last_polygon_xy:
			self._draw_polygon_and_panels()
		else:
			self._draw_placeholder()

	def _draw_placeholder(self) -> None:
		self.canvas.delete("all")
		w = max(self.canvas.winfo_width(), 200)
		h = max(self.canvas.winfo_height(), 180)
		self.canvas.create_text(
			w / 2,
			h / 2,
			text="Zoek een adres om de gebouwcontour en panelen te tonen",
			fill="#6a8299",
			font=("Segoe UI", 11),
		)

	def _draw_polygon_and_panels(self) -> None:
		if not self.last_polygon_xy:
			self._draw_placeholder()
			return

		self.canvas.delete("all")
		w = max(self.canvas.winfo_width(), 260)
		h = max(self.canvas.winfo_height(), 220)
		self._draw_grid(w, h)

		bounds = self._scene_bounds_xy()
		if bounds is None:
			self._draw_placeholder()
			return
		self._setup_fit_transform(bounds, w, h)

		for context_poly in self.last_context_polygons_xy:
			if len(context_poly) < 3:
				continue
			context_coords: List[float] = []
			for x_m, y_m in context_poly:
				x, y = self._world_to_canvas(x_m, y_m, w, h)
				context_coords.extend([x, y])
			self.canvas.create_polygon(
				context_coords,
				fill="#e3e6ea",
				outline="#c6cdd5",
				width=1,
			)

		roof_coords: List[float] = []
		for x_m, y_m in self.last_polygon_xy:
			x, y = self._world_to_canvas(x_m, y_m, w, h)
			roof_coords.extend([x, y])
		self.canvas.create_polygon(
			roof_coords,
			fill="#c3ddf6",
			outline="#14548e",
			width=2,
		)

		if self.selected_roof_edge is not None:
			a, b = self.selected_roof_edge
			x1, y1 = self._world_to_canvas(a[0], a[1], w, h)
			x2, y2 = self._world_to_canvas(b[0], b[1], w, h)
			self.canvas.create_line(x1, y1, x2, y2, fill="#ff9f1a", width=4)

		for panel in self.last_panels:
			panel_coords: List[float] = []
			for x_m, y_m in panel:
				x, y = self._world_to_canvas(x_m, y_m, w, h)
				panel_coords.extend([x, y])
			self.canvas.create_polygon(
				panel_coords,
				fill="#53b95e",
				outline="#2d7f38",
				width=1,
			)

		for no_go_poly in self.no_go_polygons:
			if len(no_go_poly) < 3:
				continue
			rect_coords: List[float] = []
			for x_m, y_m in no_go_poly:
				x, y = self._world_to_canvas(x_m, y_m, w, h)
				rect_coords.extend([x, y])
			self.canvas.create_polygon(
				rect_coords,
				fill="#ef5e54",
				outline="#9f1b1b",
				stipple="gray25",
				width=2,
			)

		if self.is_drawing_no_go and self.no_go_start_canvas and self.no_go_current_canvas:
			start_world = self._canvas_to_world(*self.no_go_start_canvas)
			end_world = self._canvas_to_world(*self.no_go_current_canvas)
			if start_world and end_world:
				preview_poly = self._create_oriented_no_go_polygon(
					start_world,
					end_world,
					self.last_orientation_deg,
				)
				preview_coords: List[float] = []
				for x_m, y_m in preview_poly:
					x, y = self._world_to_canvas(x_m, y_m, w, h)
					preview_coords.extend([x, y])
				self.canvas.create_polygon(
					preview_coords,
					outline="#ba1515",
					fill="",
					dash=(4, 3),
					width=2,
				)

		self.canvas.create_text(
			12,
			12,
			anchor="nw",
			text=f"Orientatie panelen: {self.last_orientation_deg:.1f} graden",
			fill="#43617d",
			font=("Segoe UI", 9, "bold"),
		)
		self.canvas.create_text(
			12,
			30,
			anchor="nw",
			text="Muiswiel = zoom, slepen = pannen, no-go = rood vak, randselectie = klik op dakrand",
			fill="#43617d",
			font=("Segoe UI", 9),
		)

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
