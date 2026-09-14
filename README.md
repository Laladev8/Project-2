# Docker Request Lab

İki Python tətbiqi bir-birinə HTTP sorğuları göndərir. Prometheus metrikləri
toplayır, Grafana isə hazır dashboard göstərir. Tətbiqlər Python standard
library ilə işləyir; əlavə pip paketi lazım deyil.

## Tələblər və işə salmaq

Docker Engine + Docker Compose v2 (2.15 və yuxarı) və ya Linux containers rejimində
Docker Desktop. Container CPU/RAM panelləri üçün cgroup v2 tələb olunur.
Docker üçün ən azı 2 CPU və 3 GB RAM ayırmaq rahatdır.

```bash
cp .env.example .env
# .env faylında GRAFANA_ADMIN_PASSWORD üçün öz parolunu yaz.
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

| Servis | Ünvan | CPU limiti | RAM limiti |
|---|---|---|---|
| app-a | http://localhost:8001 | 0.5 core | 256 MiB |
| app-b | http://localhost:8002 | 0.5 core | 256 MiB |
| Prometheus | http://localhost:9090 | 0.5 core | 512 MiB |
| Grafana | http://localhost:3000 | 0.5 core | 512 MiB |

Grafana istifadəçisi: `admin`. Parol: `.env` daxilində yazdığın parol.
Dashboard: http://localhost:3000/d/docker-request-lab/docker-request-lab
və ya **Dashboards → Docker Lab → Docker Request Lab**.
İlk qrafiklər üçün 30–60 saniyə gözlə.

Portlar lokal kompüterə bağlanıb. Uzaq VM-də işlədirsənsə, SSH tunnel istifadə et:

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 \
    -L 8001:127.0.0.1:8001 -L 8002:127.0.0.1:8002 user@VM_IP
```

## Necə işləyir?

- `app-a` generatoru `http://app-b:8000/work` ünvanına sorğu göndərir.
- `app-b` generatoru `http://app-a:8000/work` ünvanına sorğu göndərir.
- `/work` sorğunu emal edib cavab verir. Özü əlavə sorğu başlatmır.
- Başlanğıcda hər tətbiq saniyədə təxminən 5 sorğu göndərir.
- Prometheus hər 5 saniyədən bir hər tətbiqin `/metrics` endpoint-ini oxuyur.
- Grafana datasource və dashboard-u fayllardan avtomatik yaradır.

## Yük ssenariləri

Komandaları layihə qovluğundan icra et:

```bash
# Normal: hər istiqamətə 5 RPS, hər sorğuya 5 ms CPU, 16 MiB əlavə yaddaş
bash scripts/load.sh normal

# App-a -> app-b: hədəf 80 RPS; app-b: 20 ms CPU/request və 128 MiB yaddaş
bash scripts/load.sh high

# App-b cavablarının təxminən 30%-i HTTP 503 olsun
bash scripts/load.sh errors

# App-b hər sorğuya əlavə 1 saniyə gözləmə əlavə etsin
bash scripts/load.sh slow

# Hər iki generator dayansın; artıq göndərilmiş sorğular tamamlanır
bash scripts/load.sh stop

# Bütün parametrləri başlanğıc vəziyyətinə qaytar
bash scripts/load.sh normal
```

`errors` və `slow` yalnız öz parametrlərini dəyişir, əvvəlki yük saxlanılır.
Müstəqil sınaq üçün əvvəl `normal` icra et. Hər ssenarini 1–2 dəqiqə izlə.
`high` zamanı 80 × 20 ms = 1.6 CPU core nəzəri tələb yaranır,
amma app-b limiti 0.5 core-dur. Buna görə faktiki RPS aşağı düşə,
throttling, gecikmə, ötürülməyən sorğu və timeout sayı arta bilər.
Python GIL və generatorun öz limiti də faktiki nəticəyə təsir edir.
Bu tətbiq dəqiq benchmark aləti deyil, müşahidə laboratoriyasıdır.

## Parametrləri ayrıca dəyişmək

```bash
# App-a-nın app-b-yə göndərdiyi yükü artır
curl --fail -X POST http://localhost:8001/config \
  -H 'Content-Type: application/json' -d '{"rps":50}'

# App-b-nin qəbul etdiyi hər sorğunu ağırlaşdır
curl --fail -X POST http://localhost:8002/config \
  -H 'Content-Type: application/json' \
  -d '{"cpu_ms":20,"memory_mb":128,"delay_ms":100,"error_rate":0.1}'

curl http://localhost:8001/config
curl http://localhost:8002/metrics
```

| Parametr | Aralıq | Mənası |
|---|---|---|
| `rps` | 0–500 | Bu tətbiqin qarşı tərəfə göndərmək istədiyi sorğu/saniyə |
| `cpu_ms` | 0–500 | Bu tətbiqdə qəbul edilən hər sorğu üçün CPU vaxtı |
| `memory_mb` | 0–160, tam ədəd | Tətbiqin saxladığı əlavə yaddaş, MiB |
| `delay_ms` | 0–3000 | Hər daxil olan sorğu üçün əlavə gözləmə |
| `error_rate` | 0–1 | HTTP 503 cavabı ehtimalı; 0.3 = 30% |

Yaddaş hər sorğu üçün yenidən ayrılmır, tətbiqdə bir buffer saxlanılır.
Ümumi container RAM istifadəsinə Python, thread-lər və digər xərclər də daxildir.
Yaddaş azaldıldıqda allocator səbəbindən RSS dərhal tam enməyə bilər.
Eyni anda ən çox 16 outbound və 16 aktiv inbound iş sorğusu var.
Generatorun bütün slotları doludursa yeni cəhd ötürülmür (`dropped`).
Outbound timeout 5 saniyədir. Timeout olmuş iş serverdə tamamlanmağa davam edə bilər.
Runtime parametrləri restart-dan sonra başlanğıc qiymətlərinə qayıdır.

## Grafana-da nə görünür?

Target UP/DOWN, qəbul edilən RPS, göndərilən uğurlu/xətalı RPS, P95 gecikmə,
HTTP 503 faizi, proses CPU-su, container CPU limiti istifadəsi, container RAM,
RAM limiti istifadəsi, throttling, ötürülməyən cəhdlər və proses uptime.

CPU limitinin 100%-i 0.5 core istifadə deməkdir. Process CPU panelində 100%
bir tam core deməkdir. Container RAM `memory.current` dəyəridir;
Docker stats cache çıxıldığı üçün fərqli göstərə bilər.
P95 yalnız `/work` emal müddətidir, şəbəkə və outbound timeout müddəti daxil deyil.
RPS sıfır olduqda P95 və xəta faizi üçün məlumat olmaması normaldır.
Target UP/DOWN HTTP `/health` deyil, Prometheus scrape nəticəsidir.

Container göstəriciləri üçün `/sys/fs/cgroup` daxilindən cgroup v2 oxunur;
Docker socket və privileged cAdvisor lazım deyil. Cgroup v1 istifadə edilərsə
container panelləri boş qalır, process CPU/RSS və HTTP metrikləri işləyir.
Dashboard-dakı **Cgroup v2 available** paneli bunu göstərir.

## Restart və log müşahidəsi

```bash
docker compose restart app-b
docker compose logs -f --tail=50 app-a app-b
docker stats
```

Restart zamanı peer xətaları və uptime sıfırlanması görünə bilər.
Çox qısa restart 5 saniyəlik scrape intervalı arasında olarsa DOWN görünməyə bilər.
Loglar JSON formatındadır: başlanğıc, config dəyişiklikləri və hər 10 saniyədə
sayğac xülasəsi. Loglar Docker stdout-a gedir; Grafana log paneli/Loki daxil deyil.
CPU limitinə çatmaq öz-özünə restart yaratmır. `unhealthy` statusu da Compose
tərəfindən avtomatik restart edilmir. Proses çıxarsa `unless-stopped` tətbiq olunur.

## Resurs limitini dəyişmək

`docker-compose.yml` içində `x-app` altında `cpus`, `mem_limit`,
`memswap_limit` dəyiş. Sonra hər iki tətbiqi yenidən yarat:

```bash
docker compose up -d --force-recreate app-a app-b
```

`memswap_limit` RAM limiti ilə eyni olduğundan tətbiq container-ləri əlavə swap
istifadə etmir. API üçün 160 MiB buffer maksimumu 256 MiB container limitinə
uyğun seçilib; RAM limitini azaltsan böyük buffer OOM yarada bilər.


## Dayandırmaq

```bash
docker compose down
```

Grafana və Prometheus məlumatları named volume-larda qalır. `docker compose down -v`
bu məlumatları da silir. `.env` parolunu dəyişmək mövcud Grafana volume-undakı
admin parolunu dəyişmir; parolu Grafana UI-dən dəyişmək lazımdır.

## GitHub-a push

GitHub-da boş repo yarat; sonra bu qovluqda:

```bash
git init
git add .
git commit -m "Add two-app Docker monitoring lab"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/docker-request-lab.git
git push -u origin main
```

`.env` gitignore-dadır; repoya parol daxil etmə. Image versiyaları konkret
tag-larla sabitlənib; layihə lokal laboratoriya üçündür.

## Rəsmi sənədlər

- https://docs.docker.com/reference/compose-file/services/
- https://prometheus.io/docs/prometheus/latest/configuration/configuration/
- https://grafana.com/docs/grafana/latest/administration/provisioning/
