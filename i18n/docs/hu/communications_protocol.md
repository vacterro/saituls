# Kommunikációs protokoll ötletek értékeléséhez

A Windows helyi menü testreszabási ökoszisztémájában dolgozó összes későbbi ügynöknek be kell tartania ezt a strukturált protokollt, amikor javasolt ötleteket vitat meg, értékel vagy finomít.

## 1. A "Steel-Man" doktrína
Mielőtt egy ötletet megvitatna, az értékelő ügynöknek meg kell alkotnia az eredeti javaslat legerősebb lehetséges változatát.
- A központi értékajánlatot az eredeti szerzőnél világosabban megfogalmazni.
- Legalább egy ki nem mondott előnyt azonosítani a megközelítésből.

## 2. Red-Teaming (sebezhetőségi értékelés)
Miután az ötletet megerősítették, az ügynököknek a következő vektorok mentén kell megvitatniuk:
- **Ökoszisztéma-illeszkedés:** Ez natív helyi menü eszköznek érződik, vagy teljes alkalmazás akar lenni?
- **Teljesítmény:** Mi történik, ha véletlenül egy 100 000 fájlt tartalmazó könyvtáron fut?
- **Rombolás:** Van-e kockázata visszafordíthatatlan adatvesztésnek?
- **Függőségi teher:** Túlzott külső függőségeket igényel (pl. hatalmas Python-könyvtárak vagy nem telepített binárisok)?

## 3. A cáfolat formátuma
Minden kritikát a következőképpen kell felépíteni:
- **Hipotézis:** Mit kíván megoldani az ötlet.
- **Sebezhetőség:** Az azonosított konkrét hiba vagy kockázat.
- **Alternatív megfogalmazás:** Javasolt fordulat, amely megtartja az értéket és csökkenti a kockázatot.

## 4. A végleges ítélet mechanizmusa
Az ötleteket nem szabad egyenesen elutasítani fordulat javaslata nélkül, kivéve, ha katasztrofális kockázatot jelentenek a fájlrendszerre (pl. nem követett rekurzív törlés).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
