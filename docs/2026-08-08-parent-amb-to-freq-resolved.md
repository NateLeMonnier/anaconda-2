# parent_amb rows now resolving at state/country level (08-06 -> 08-08)

189 rows that were `parent_amb` in the 08-06 run now resolve as `freq_resolved` at
state (L6) or country (L8) level in 08-08. Same root cause throughout:
`_disambiguate_by_frequency` now breaks ties on the state/country candidate set
that used to come back tied.

## Summary

| Verdict   | Rows    | Freq    | Meaning                                                                                             |
| ---       | ---     | ---     | ---                                                                                                 |
| GOOD      | 141     | 521     | Exact state/country name beat decoy small towns sharing the word. Correct upgrade.                  |
| FLAG      | 45      | 161     | Tied against a second real, comparably prominent place — frequency isn't a real disambiguator here. |
| CAUTION   | 3       | 9       | Name collision exists in principle; correct in this sample by luck of context.                      |
| **Total** | **189** | **691** |                                                                                                     |

---

## FLAG — New York (state vs. city)

Tied set had both `New York, USA` (state) and `New York City, New York, USA` (city) — both real, both common. Frequency picked the state, but many of these originals are clearly Manhattan addresses.

33 rows, 93 total frequency.

| freq | original                                                                        | resolved to     |
| ---  | ---                                                                             | ---             |
| 11   | Park Row Building, New York                                                     | New York, State |
| 9    | Bartholdi Hotel, New York                                                       | New York, State |
| 6    | corner of Hudson and Dominick streets, New York, New York                       | New York, State |
| 6    | elevated railroad station at Rector street, New York                            | New York, State |
| 5    | New Grand Hotel, New York                                                       | New York, State |
| 4    | Milcottville, Milcottville, NY                                                  | New York, State |
| 4    | Buckmantown, New York                                                           | New York, State |
| 4    | 60 Lexington Street, New York                                                   | New York, State |
| 3    | 1217 New York ave., New York                                                    | New York, State |
| 3    | The New York Nursery and Childs hospital, 101 West Sixty first street, New York | New York, State |
| 3    | Murray Hill Turkish baths, No. 113 West Fortysecond street, New York            | New York, State |
| 3    | 520 West One Hundred and Fortyfourth Street, New York                           | New York, State |
| 2    | Fulton Theater, New York                                                        | New York, State |
| 2    | West Side apartment?, New York                                                  | New York, State |
| 2    | sinking steamer Washington, New York                                            | New York, State |
| 2    | sweater shop, 10 and 12 Montgomery Street, New York, New York                   | New York, State |
| 2    | West 5th street, New York                                                       | New York, State |
| 2    | courtyard beneath the windows of her Fifth avenue apartment, New York           | New York, State |
| 2    | Atlantic Avenue Hotel, New York                                                 | New York, State |
| 2    | No. 14 Thompson Street, tenement, New York                                      | New York, State |
| 2    | bathroom of residence on East Fortieth street, New York                         | New York, State |
| 2    | 14 Wort 58th street, New York                                                   | New York, State |
| 2    | American army hospital, New York                                                | New York, State |
| 1    | Residence at 120 East 90th St., New York                                        | New York, State |
| 1    | Oliver and Madison streets, New York                                            | New York, State |
| 1    | Elevator, Emigrant Savings Bank, New York                                       | New York, State |
| 1    | Sturtevant house, Sixth avenue and Twenty eighth street, New York               | New York, State |
| 1    | New York (saloon), New York                                                     | New York, State |
| 1    | Fourteenth Street tenement, New York                                            | New York, State |
| 1    | intersection of Fifth Avenue and Fortieth Street, New York                      | New York, State |
| 1    | corridor of the New York Life Insurance Building, New York, New York            | New York, State |
| 1    | 1904 Market street, New York                                                    | New York, State |
| 1    | Twentyfifth street, New York                                                    | New York, State |

## FLAG — Washington (state vs. D.C.)

Tied set had both `Washington, USA` (state) and `Washington D.C., USA` — both real and common, no signal at this level to pick between them.

11 rows, 60 total frequency.

| freq | original                                                  | resolved to       |
| ---  | ---                                                       | ---               |
| 16   | 5669 Van Dyke road, Washington                            | Washington, State |
| 8    | 100 Pennsylvania Avenue Southeast, Washington             | Washington, State |
| 7    | Slatestone Road community, Rt. 4, Washington, Washington  | Washington, State |
| 7    | Auburn hospital, Washington                               | Washington, State |
| 6    | home of his son, 406 southeast Twelfth street, Washington | Washington, State |
| 4    | home of Rev. S. K. Coats, Washington                      | Washington, State |
| 3    | capilla particular del Cardenal Martinelli, Washington    | Washington, State |
| 3    | 1121 street, Washington                                   | Washington, State |
| 3    | 315 South Iowa Avenue, Washington                         | Washington, State |
| 2    | Kenmore hotel room, Washington                            | Washington, State |
| 1    | Ridgewood Manor Rehabilitation Center, Washington         | Washington, State |

## FLAG — Bristol (likely wrong pick)

Picked Bristol, England over four US Bristol towns (CT/IL/TN/RI) despite a US street-address format in the original.

1 rows, 8 total frequency.

| freq | original                            | resolved to           |
| ---  | ---                                 | ---                   |
| 8    | 442 Lafayette St., Bristol, Bristol | Bristol, Federal city |

## CAUTION — Georgia (state vs. country)

State vs. country name collision exists in principle; both sample rows have GA-city context (Macon, Fort Valley, Newnan) confirming state is correct here.

3 rows, 9 total frequency.

| freq | original                                             | resolved to    |
| ---  | ---                                                  | ---            |
| 5    | Devils Dip (road from Macon to Fort Valley), Georgia | Georgia, State |
| 2    | Carrothen, Ga                                        | Georgia, State |
| 2    | Lake (the Lake at Newnan), Georgia                   | Georgia, State |

## GOOD — exact name beat decoy candidates

141 rows, 521 total frequency.

| freq | original                                                                      | resolved to                          |
| ---  | ---                                                                           | ---                                  |
| 24   | Warren Air Force Base, Wyoming                                                | Wyoming, State                       |
| 19   | South Main Street, Poland                                                     | Poland, Country                      |
| 18   | Alen, Norway                                                                  | Norway, Country                      |
| 15   | Bourbanis, Illinois                                                           | Illinois, State                      |
| 15   | Patentville, La                                                               | Louisiana, State                     |
| 13   | Kilkinney, Ireland                                                            | Ireland, Country                     |
| 12   | Bridges community (home of her daughter), Arkansas                            | Arkansas, State                      |
| 11   | Houston (home of daughter, Mrs. P.O. Dale), Texas                             | Texas, State                         |
| 11   | Rt. 3 Navasota, Texas                                                         | Texas, State                         |
| 11   | Hes sian, Germany                                                             | Germany, Country                     |
| 10   | Breadstadt, Germany                                                           | Germany, Country                     |
| 9    | Reichsachsen Kries Eschwege, Germany                                          | Germany, Country                     |
| 9    | Kennedy General Hospital, Tennessee                                           | Tennessee, State                     |
| 9    | Unter Schozenthal, Oberant Leaknang, Germany                                  | Germany, Country                     |
| 8    | Hunters station, near Coeur Alene City, Idaho                                 | Idaho, State                         |
| 7    | Mexican-style canyon ranch home, California                                   | California, State                    |
| 7    | rural route Kokomo, Indiana                                                   | Indiana, State                       |
| 7    | Marano Marches, Italy                                                         | Italy, Country                       |
| 6    | Cambria Security, Pennsylvania                                                | Pennsylvania, State                  |
| 6    | home at Sucker Lake, Idaho                                                    | Idaho, State                         |
| 6    | 408 Washington St., Oregon                                                    | Oregon, State                        |
| 6    | Japonica, Texas                                                               | Texas, State                         |
| 6    | Chapel of Chateauraux Air Force base, Chateauraux, France                     | France, Country                      |
| 6    | Guitenburg, la.                                                               | Louisiana, State                     |
| 6    | Salt Philpin, Colorado                                                        | Colorado, State                      |
| 6    | Lake City hospital, Minn.                                                     | Minnesota, State                     |
| 6    | Mile 165, Alaska Highway, Alaska                                              | Alaska, State                        |
| 5    | accident scene near intersection of U.S. 281 and Farm Market Road 1177, Texas | Texas, State                         |
| 5    | Las Gabriel, Calif.                                                           | California, State                    |
| 5    | Tempe Hospital?, Arizona                                                      | Arizona, State                       |
| 5    | Warriors Mark valley (old Trimble homestead), Pennsylvania                    | Pennsylvania, State                  |
| 5    | 40 Michigan, Michigan                                                         | Michigan, State                      |
| 5    | Roscon monmon county, Ireland                                                 | Ireland, Country                     |
| 5    | 1362 Parkwood Place N.W., District of Columbia                                | Washington D.C., Federal district    |
| 5    | police court, London                                                          | London, Federal city                 |
| 5    | Adams County Courthouse, Pennsylvania                                         | Pennsylvania, State                  |
| 5    | edge of Big Arroyo canyon, Kern River canyon, California                      | California, State                    |
| 4    | Hovatzdale, Pennsylvania                                                      | Pennsylvania, State                  |
| 4    | 1018 Pennsylvania avenue, Pennsylvania                                        | Pennsylvania, State                  |
| 4    | 509 Locust, Montana                                                           | Montana, State                       |
| 4    | Skyron Bucks County Airport, Pennsylvania                                     | Pennsylvania, State                  |
| 4    | Berkeley home of Scotty Allan, California                                     | California, State                    |
| 4    | Brotman Medical Center, California                                            | California, State                    |
| 4    | Monongahela hospital, Pennsylvania                                            | Pennsylvania, State                  |
| 4    | Kirkweille, Mo.                                                               | Missouri, State                      |
| 4    | FeremiaRiga, Romania                                                          | Romania, Country                     |
| 4    | water-filled tank on the farm, Arkansas                                       | Arkansas, State                      |
| 3    | Nebel Amrum, Germany                                                          | Germany, Country                     |
| 3    | Rictimony, Ind.                                                               | Indiana, State                       |
| 3    | Chiracahua Mountains, Arizona                                                 | Arizona, State                       |
| 3    | 510 West Green Street, Illinois                                               | Illinois, State                      |
| 3    | Parkers switch, Indiana                                                       | Indiana, State                       |
| 3    | Kilpourn Crry, Wis., Wis.                                                     | Wisconsin, State                     |
| 3    | No. 203 General Hospital, England                                             | England, Country                     |
| 3    | Lindsay warehouse, Montana                                                    | Montana, State                       |
| 3    | Compton Rectory, Surrey                                                       | Surrey, Administrative county        |
| 3    | Weigis, Tex.                                                                  | Texas, State                         |
| 3    | Xavier Hospital, 2645a Jackson, Ill.                                          | Illinois, State                      |
| 3    | Kitzenten, Germany                                                            | Germany, Country                     |
| 3    | Honey Grove (at his home), Texas                                              | Texas, State                         |
| 3    | La imeca, Texas                                                               | Texas, State                         |
| 3    | Municipal hospital, Port St. Joseph, Fla.                                     | Florida, State                       |
| 3    | CFB Suffield, Alberta                                                         | Alberta, Province                    |
| 3    | 5 Kimberly Lane, Apt. 326, Mexico                                             | México, Country                      |
| 3    | home at 903 13th street northwest, District of Columbia                       | Washington D.C., Federal district    |
| 3    | corner of a house / Austin, Texas, Texas                                      | Texas, State                         |
| 2    | Dupageco, Ill                                                                 | Illinois, State                      |
| 2    | Uaetel grande, Italy                                                          | Italy, Country                       |
| 2    | Dowdy home, La.                                                               | Louisiana, State                     |
| 2    | Zion Nursing Home, Illinois                                                   | Illinois, State                      |
| 2    | Frankfort apartment, Indiana                                                  | Indiana, State                       |
| 2    | Hook Church, Surrey                                                           | Surrey, Administrative county        |
| 2    | Punxantawney, Pennsylvania                                                    | Pennsylvania, State                  |
| 2    | Maine crash site, Maine                                                       | Maine, State                         |
| 2    | Philadelphia division of Baltimore and Ohio railroad near Leslie Station, Md  | Maryland, State                      |
| 2    | Lake Norfolk east shore, Arkansas                                             | Arkansas, State                      |
| 2    | hospital in Shanghai outskirts, Shanghai                                      | Shanghai, Municipality               |
| 2    | Benna Vista, Tennessee                                                        | Tennessee, State                     |
| 2    | Interstate 70 just west of the Eisenhower tunnel, Colorado                    | Colorado, State                      |
| 2    | Cobenz, Germany                                                               | Germany, Country                     |
| 2    | camp chapel at In diantown Gap Military Reservation, Pennsylvania             | Pennsylvania, State                  |
| 2    | Bar Singbourne, England                                                       | England, Country                     |
| 2    | Kilgores station, Arkansas                                                    | Arkansas, State                      |
| 2    | outside his home, Egypt                                                       | Egypt, Country                       |
| 2    | Interstate 57, one mile north of the Kankakee Will County line, Illinois      | Illinois, State                      |
| 2    | Cristonia, Texas, Texas                                                       | Texas, State                         |
| 2    | Shereville, Ind.                                                              | Indiana, State                       |
| 2    | Virginia Legislature, Virginia                                                | Virginia, State                      |
| 2    | Westerzoland, Sweden, Sweden                                                  | Sweden, Country                      |
| 2    | San Graben, Calif.                                                            | California, State                    |
| 2    | Independence Street, PA                                                       | Pennsylvania, State                  |
| 2    | Terra Nova Hotel, Jamaica                                                     | Jamaica, Country                     |
| 2    | Londonerrs, Ireland                                                           | Ireland, Country                     |
| 2    | BadenWeller, Germany                                                          | Germany, Country                     |
| 2    | St. Aloysius Church, District of Columbia                                     | Washington D.C., Federal district    |
| 2    | near Rigel Station, Illinois                                                  | Illinois, State                      |
| 1    | 316 S. 17th St., Texas                                                        | Texas, State                         |
| 1    | Tiron, France                                                                 | France, Country                      |
| 1    | Eaton Naples, Michigan                                                        | Michigan, State                      |
| 1    | Rock Alow Springs, Virginia                                                   | Virginia, State                      |
| 1    | Cheyenne reservation, Montana                                                 | Montana, State                       |
| 1    | Anaconda Mine shaft, Montana                                                  | Montana, State                       |
| 1    | Plaistow Marshes, Essex                                                       | Essex, Administrative county         |
| 1    | Alliance post office, Mayd county, Ky.                                        | Kentucky, State                      |
| 1    | Jaycowie, Indiana                                                             | Indiana, State                       |
| 1    | Hovtzdile, Pa.                                                                | Pennsylvania, State                  |
| 1    | Capitol steps, Montana                                                        | Montana, State                       |
| 1    | near Fort Worth in the Burleson community, Tex.                               | Texas, State                         |
| 1    | Oxnard motel, California                                                      | California, State                    |
| 1    | St. Harlan, Ky., Ky.                                                          | Kentucky, State                      |
| 1    | Chicago River near State Street Bridge, Illinois                              | Illinois, State                      |
| 1    | Wol fwye, Hertfordshire                                                       | Hertfordshire, Administrative county |
| 1    | Haveiaburg, Pa                                                                | Pennsylvania, State                  |
| 1    | Diyatalawa, Ceylon                                                            | Sri Lanka, Country                   |
| 1    | Gnano Rampa, Mich.                                                            | Michigan, State                      |
| 1    | Telelmebir, Egypt                                                             | Egypt, Country                       |
| 1    | Obama, Oregon                                                                 | Oregon, State                        |
| 1    | Isertardt, Oregon                                                             | Oregon, State                        |
| 1    | Catholic Chapel at the Chitose Air Force Base, Japan                          | Japan, Country                       |
| 1    | Tidicote, Pa                                                                  | Pennsylvania, State                  |
| 1    | Leytonshire, London                                                           | London, Federal city                 |
| 1    | East One Hundred and Twelfth Street, Italy                                    | Italy, Country                       |
| 1    | Johnston's home, Ky.                                                          | Kentucky, State                      |
| 1    | Stepstons creek, Kentucky                                                     | Kentucky, State                      |
| 1    | Camp Polk station hospital, La.                                               | Louisiana, State                     |
| 1    | Braunsport, Ore.                                                              | Oregon, State                        |
| 1    | Pernax, Minn                                                                  | Minnesota, State                     |
| 1    | Galvestus, TX                                                                 | Texas, State                         |
| 1    | deep gulch six miles this side of the White River Agency, Colorado            | Colorado, State                      |
| 1    | San Francisco (hotel window), California                                      | California, State                    |
| 1    | Kwxtontstows, Ind.                                                            | Indiana, State                       |
| 1    | 12 miles south of Fredericksburg on US 1, Virginia                            | Virginia, State                      |
| 1    | RD 3 Uniontown, PA                                                            | Pennsylvania, State                  |
| 1    | Hlesthesville, Va                                                             | Virginia, State                      |
| 1    | Hotel Tostaine, Tennessee                                                     | Tennessee, State                     |
| 1    | Broomsword, Pa                                                                | Pennsylvania, State                  |
| 1    | CriLicoTruk, Mo.                                                              | Missouri, State                      |
| 1    | Bray Sur Somme, France                                                        | France, Country                      |
| 1    | local Pennsylvania yard, Pennsylvania                                         | Pennsylvania, State                  |
| 1    | Neil Blacks Station, Glenorailston, Victoria                                 | Victoria, Australia                  |
| 1    | 601 City, Pa                                                                  | Pennsylvania, State                  |
