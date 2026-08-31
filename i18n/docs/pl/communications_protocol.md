# Protokół komunikacji do oceny pomysłów

Wszyscy kolejni agenci pracujący w ekosystemie dostosowywania menu kontekstowego Windows muszą przestrzegać tego ustrukturyzowanego protokołu przy kwestionowaniu, ocenie lub udoskonalaniu proponowanych pomysłów.

## 1. Doktryna "Steel-Mana"
Przed zakwestionowaniem pomysłu agent oceniający musi skonstruować najsilniejszą możliwą wersję pierwotnej propozycji.
- Przedstawić główną propozycję wartości jaśniej niż oryginalny autor.
- Wskazać co najmniej jedną niewypowiedzianą korzyść podejścia.

## 2. Red-Teaming (ocena podatności)
Gdy pomysł zostanie wzmocniony, agenci muszą go zakwestionować wzdłuż następujących wektorów:
- **Dopasowanie do ekosystemu:** Czy to wygląda jak natywne narzędzie menu kontekstowego, czy próbuje być pełną aplikacją?
- **Wydajność:** Co się stanie, jeśli zostanie przypadkowo uruchomione na katalogu ze 100 000 plików?
- **Destrukcyjność:** Czy istnieje ryzyko nieodwracalnej utraty danych?
- **Nadmiar zależności:** Czy wymaga to nadmiernych zewnętrznych zależności (np. ogromnych bibliotek Pythona lub niezainstalowanych plików binarnych)?

## 3. Format kontrargumentu
Każda krytyka musi być ustrukturyzowana w następujący sposób:
- **Hipoteza:** Co pomysł ma rozwiązać.
- **Podatność:** Konkretna wada lub ryzyko.
- **Alternatywne sformułowanie:** Proponowany zwrot, który zachowuje wartość i zmniejsza ryzyko.

## 4. Mechanizm ostatecznego werdyktu
Pomysłów nie wolno odrzucać bez zaproponowania zwrotu, chyba że stanowią katastrofalne zagrożenie dla systemu plików (np. nieśledzone rekurencyjne usuwanie).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
