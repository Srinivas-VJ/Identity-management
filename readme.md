# Decentralised Identity Management using Blockchain

## Setup VON network (Indy public ledger sandbox)

```
cd von-network
./manage build
./manage start --logs
```

This builds docker images for VON and starts the indy network (keep it runnin bois).
You can see the ledger at http://localhost:9000

## Alice Faber demo

open two terminals one for Alice and one for Faber and run their images

```
cd aries-cloudagent-python/demo
./run_demo faber
./run_demo alice
```

In the terminal you'll have a link for the administration API here we can see all the endpoints (swagger UI) playaround and checkout the apis once.

## To Check The Performance

You can get the performance bechmark by running performance . To do this, navigate to demo

```
cd aries-cloudagent-python/demo
```

Then run the performance agent

```
./run_demo performance
```

## To push changes in submodules

To push changes in submodules first navigate to the submodule directory and do a git push

```
cd path-to-submodule (von-network or aries-cloudagent and so on)
git add .
git commit -m "changes in submodule"
git push origin HEAD:main
```

Then go back to the main folder and do a git push

```
cd ..
git add .
git commit -m "changes in main repo"
git push
```
