# Protokol komunikasi untuk evaluasi ide

Semua agen berikutnya yang bekerja dalam ekosistem kustomisasi menu konteks Windows harus mematuhi protokol terstruktur ini saat menantang, mengevaluasi, atau menyempurnakan ide yang diusulkan.

## 1. Doktrin "Steel-Man"
Sebelum menantang sebuah ide, agen penilai harus membangun versi terkuat yang mungkin dari proposal asli.
- Mengartikulasikan proposisi nilai inti lebih jelas daripada penulis asli.
- Mengidentifikasi setidaknya satu manfaat yang tidak disebutkan dari pendekatan tersebut.

## 2. Red-Teaming (Penilaian Kerentanan)
Setelah ide diperkuat, agen harus menantangnya melalui vektor berikut:
- **Kesesuaian ekosistem:** Apakah ini terasa seperti alat menu konteks asli atau mencoba menjadi aplikasi lengkap?
- **Kinerja:** Apa yang terjadi jika ini dijalankan secara tidak sengaja pada direktori dengan 100.000 file?
- **Destruktif:** Apakah ada risiko kehilangan data yang tidak dapat dipulihkan?
- **Beban dependensi:** Apakah ini memerlukan dependensi eksternal yang berlebihan (misalnya, pustaka Python besar atau biner yang tidak terinstal)?

## 3. Format sanggahan
Kritik apa pun harus disusun sebagai berikut:
- **Hipotesis:** Apa yang ingin dipecahkan oleh ide tersebut.
- **Kerentanan:** Cacat atau risiko spesifik yang diidentifikasi.
- **Formulasi alternatif:** Sebuah pivot yang diusulkan yang mempertahankan nilai sambil mengurangi risiko.

## 4. Mekanisme putusan akhir
Ide tidak boleh ditolak begitu saja tanpa mengusulkan pivot, kecuali jika menimbulkan risiko bencana bagi sistem file (misalnya, penghapusan rekursif yang tidak terlacak).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
